"""klipperformac command-line interface: setup | up | down | killall | status |
logs | serial | update | open | doctor | timing | backup | cam | batteryguard |
powerloss | login | supervise | uninstall."""
import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import (batteryguard, cam, configgen, detect, installer, launchd, paths,
               powerloss, process, shaper, versions)


def _die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


# Commands that mutate the stack take the lock; restart re-enters cmd_up, so
# the holder handles live here for the lifetime of the process.
_LOCK_HELD = []


@contextlib.contextmanager
def _cli_lock():
    """flock(run/cli.lock) around the commands that change the stack.

    Two `up` at once (GUI double-click + terminal, TUI + CLI) used to both pass
    the "is it running" test and both spawn a full stack: two Moonrackers on
    one klippy socket, and a state.json whose pids do not own the ports - after
    which `status` lies in both directions and `down` leaves half of it running.
    The lock is advisory and per-kernel: it dies with whatever process held it,
    so a crashed CLI never leaves the tool locked out."""
    holder = [None]

    def _acquire():
        if _LOCK_HELD:
            return  # restart -> cmd_up: already ours, never wait on ourselves
        try:
            paths.RUN.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _die("Cannot prepare {} for the command lock: {}".format(
                paths.RUN, getattr(e, "strerror", None) or e))
        try:
            fh = open(paths.CLI_LOCK, "a+")
        except OSError as e:
            _die("Cannot open {} for the command lock: {}".format(
                paths.CLI_LOCK, getattr(e, "strerror", None) or e))
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Waiting for another klipperformac command to finish...")
            fcntl.flock(fh, fcntl.LOCK_EX)
        _LOCK_HELD.append(fh)
        holder[0] = fh

    try:
        _acquire()
        yield
    finally:
        if holder[0] is not None:
            try:
                fcntl.flock(holder[0], fcntl.LOCK_UN)
            except OSError:
                pass
            _LOCK_HELD.remove(holder[0])
            holder[0].close()


def _save_settings(settings):
    """paths.save_settings() with the filesystem friction turned into one
    clean line: settings.json is shared with the SwiftUI app, so a blocked
    write (read-only home, root-owned prefs after one sudo run, a directory in
    its place) must never reach the user as a traceback."""
    try:
        paths.save_settings(settings)
    except OSError as e:
        _die("Could not write {} (your settings are unchanged): {}".format(
            paths.SETTINGS, getattr(e, "strerror", None) or e))


# A preset name becomes one filename: PRESETS/<name>.cfg, and _copy_atomic first
# writes <name>.cfg.tmp beside it. macOS caps a single path component at 255
# BYTES, so a long name used to fail inside shutil with a raw
# OSError(ENAMETOOLONG) traceback. 100 characters is the human limit; the byte
# limit is what the filesystem actually enforces (an emoji-heavy name can hit it
# far before 100 characters).
PRESET_NAME_MAX_CHARS = 100
PRESET_NAME_MAX_BYTES = 240


def _safe_name(name, what="name"):
    """Reject preset/history names that could escape their directory when
    joined onto PRESETS/history, blow the filename limit, or forge rows in the
    preset listing."""
    if not name or re.search(r"[/\\]|\.\.|^\.", name):
        _die("Invalid {} '{}': no path separators, '..', or leading dot "
             "allowed".format(what, name))
    # A newline or tab in a name is not a name: it splits `klipperformac
    # presets` (and the GUI/TUI list that parses it) into rows nobody typed.
    if re.search(r"[\x00-\x1f\x7f]", name):
        _die("Invalid {} '{}': no control characters (spaces are fine, "
             "newlines and tabs are not)".format(what, name))
    if len(name) > PRESET_NAME_MAX_CHARS or len(name.encode("utf-8")) \
            > PRESET_NAME_MAX_BYTES:
        _die("Invalid {} '{}': too long ({} characters at most). A preset name "
             "is a filename".format(what, name[:32] + "...",
                                    PRESET_NAME_MAX_CHARS))
    return name


def cmd_setup(args):
    installer.setup(with_shaper=getattr(args, "with_shaper", False),
                    install_python=getattr(args, "install_python", False))


def _moonraker_get(path, timeout=3):
    """GET on our own Moonraker REST API; None when it does not answer."""
    url = "http://localhost:{}{}".format(paths.MOONRAKER_PORT, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode(errors="replace"))
    except Exception:
        return None


def _klippy_state(timeout=3):
    """klippy's state ('ready', 'startup', 'shutdown', 'error') from Moonraker,
    or None when the stack does not answer (`printer state unknown`).

    A service-level "klipper up" only proves the python process is alive: the
    finding this answers is a printer stuck in `startup` (bad serial) or
    `error` (corrupt printer.cfg) while every command said healthy."""
    answer = _moonraker_get("/printer/info", timeout)
    if not isinstance(answer, dict):
        return None
    state = (answer.get("result") or {}).get("state")
    return state if isinstance(state, str) else None


def _our_listener_ports():
    """Ports answered by this install's service processes, tracked or not."""
    try:
        return {port for port, _pid in process.our_listeners()}
    except Exception:
        return set()


def _powerloss_stack_up():
    """Are klipper AND moonraker alive? The power-loss marker is only judged when
    they are not (powerloss.last_stop): a live stack with an open session file is
    that session, not an unclean stop. Read from process.running() - the same
    strict evidence `powerloss-test` uses before it SIGKILLs a live bench job."""
    state = process.running() or {}
    return bool(state.get("klipper") and state.get("moonraker"))


def _powerloss_session_live(state=None, live_ports=None):
    """Whether the current stack session is still alive enough that an open
    power-loss marker describes *now*, not the previous stop.

    This is deliberately broader than `_powerloss_stack_up()`: advisory surfaces
    like `status`, `doctor`, and `powerloss status` should stop surfacing stale
    reboot history as soon as our own Moonraker/UI listeners are still alive,
    even if the tracked pid ledger has gone missing. The destructive
    `powerloss-test` gate stays on the stricter helper above."""
    state = process.running() if state is None else state
    if state:
        return any(state.get(name) for name in ("klipper", "moonraker",
                                                "web_mainsail",
                                                "web_fluidd"))
    ports = _our_listener_ports() if live_ports is None else set(live_ports)
    return bool({paths.MOONRAKER_PORT, paths.WEB_PORT, paths.FLUIDD_PORT}
                & ports)


def _save_powerloss(**kwargs):
    """powerloss.save_settings() with the same one-line treatment as any other
    settings write (the file is shared with the SwiftUI app)."""
    try:
        return powerloss.save_settings(**kwargs)
    except OSError as e:
        _die("Could not write {} (your settings are unchanged): {}".format(
            paths.SETTINGS, getattr(e, "strerror", None) or e))


def _lan_conf_expected():
    """What moonraker.conf pins [server] host to right now: 'open', 'closed'
    or 'unpinned' (no config/section/key - which upstream Moonraker reads as
    0.0.0.0, i.e. open)."""
    host = configgen.server_host()
    if host is None:
        return "unpinned"
    host = host.strip()
    if host in ("127.0.0.1", "localhost", "::1") or host.startswith("127."):
        return "closed"
    if host in ("0.0.0.0", "*", "::"):
        return "open"
    return "open"


def _lan_urls(indent="  "):
    """LAN URLs to advertise, judged on evidence instead of one settings file.

    There are three places LAN state lives: settings.json (what the user asked
    for, read by our proxies when they start), moonraker.conf [server] host (what
    Moonraker binds when it starts) and the running proxies' own argv (what is
    listening right now). Advertising a URL from only the first of those is how
    `status` came to print a LAN API link that refused every connection. Each
    line below is printed only for the service that really is exposed, and a
    disagreement between the three is said out loud."""
    want = paths.lan_enabled()
    expected = _lan_conf_expected()
    running_bind = process.lan_bind_in_effect()
    ip = detect.lan_ip() if want else None
    lines = []
    if want and not ip:
        lines.append(indent + "LAN:       enabled, but this Mac has no network "
                            "address right now")
        return lines
    if want:
        if running_bind != "closed":
            lines.append(indent + "LAN UI:    http://{}:{}  (mainsail)   "
                                "http://{}:{}  (fluidd)".format(
                                    ip, paths.WEB_PORT, ip, paths.FLUIDD_PORT))
        if expected == "open":
            lines.append(indent + "LAN API:   http://{}:{}  (Orca on other "
                                  "devices)".format(ip, paths.MOONRAKER_PORT))
        if running_bind == "closed":
            lines.append(indent + "[!] The UI proxies are listening on "
                        "localhost only right now - they bind the LAN address "
                        "when they start: klipperformac restart")
    if want and expected in ("closed", "unpinned"):
        lines.append(
            indent + "[!] LAN is switched on in settings but moonraker.conf "
            "pins host to {} - the API stays localhost-only until you run: "
            "klipperformac restart".format(
                "127.0.0.1" if expected == "closed" else "nothing "
                "(upstream Moonraker defaults to 0.0.0.0)"))
    if not want and (expected == "open" or running_bind == "open"):
        lines.append(
            indent + "[!] LAN is switched OFF in settings but {} is still "
            "listening on this Mac's network address - close it now with: "
            "klipperformac restart".format(
                "moonraker.conf" if expected == "open" else "the UI proxies"))
    return lines


def cmd_up(args):
    with _cli_lock():
        _cmd_up(args)
        _guard_autostart()


def _guard_autostart():
    """Bring the battery watchdog back with the stack when the user switched it
    on. This command never ENABLES anything: `up` only honours the setting
    `klipperformac batteryguard on` wrote, and stays silent when it says off."""
    try:
        if not batteryguard.battery_settings().get("enabled"):
            return
        ok, lines = batteryguard.start()
    except Exception as e:
        # Telemetry must never be the reason a stack that came up looks broken.
        print("[!] battery guard: not started ({})".format(e))
        return
    cfg = batteryguard.battery_settings()
    if ok:
        print("Battery guard: on (pause {}%, critical {}%)".format(
            cfg["pauseAt"], cfg["criticalAt"]))
    else:
        print("\n".join(lines))


def _cmd_up(args):
    paths.ensure_dirs()
    if not paths.VENV_PY.exists():
        _die("Not set up yet. Run: klipperformac setup")
    if configgen.sync_data_paths():
        print("Updated config paths for the current data folder.")
    state = process.running()
    if state is not None and any(state.values()):
        dead_core = [n for n in ("klipper", "moonraker") if not state.get(n)]
        if dead_core:
            # "Already running." here used to hide a stack whose klipper or
            # Moonraker had died: every port dead, nothing to repair. klipper
            # and moonraker are not restarted quietly, because a live klipper
            # with a print running must never be killed by a status command -
            # so this fails loudly and names the repair.
            _die("[!] Stack is half stopped ({} DOWN) but still tracked. "
                 "Repair it with: klipperformac restart".format(
                     ", ".join(dead_core)))
        dead_ui = [n for n in process.SERVICES
                   if n not in ("klipper", "moonraker") and not state.get(n)]
        if dead_ui:
            print("[!] Partially stopped ({} DOWN): restarting those services "
                  "only, the running klipper/moonraker are left alone."
                  .format(", ".join(dead_ui)))
            process.repair(state)
            _want_up("up (repair)")
        else:
            print("Already running.")
            # The intent is recorded even when there was nothing to do: the
            # whole point of the want file is that a later crash of a stack the
            # user last touched with `up` is a crash, not a refusal.
            _want_up("up")
            return
    else:
        note = shaper.interpreter_for_klipper()[1]
        # The verdict of the session before this one, read BEFORE start() rolls
        # it into history: at up-time is the moment the question "did the power
        # go out, or did somebody just type `down`?" still has an answer worth
        # giving the user before they start a new print on the same machine.
        prior = powerloss.verdict_lines(stack_up=False)
        if not process.start():
            sys.exit(1)  # start() said why (port held, record not writable)
        _want_up("up")
        if note:
            print(note)
        print("Klipper stack starting:")
        print("  Mainsail:  " + paths.ui_url("mainsail"))
        print("  Fluidd:    " + paths.ui_url("fluidd"))
        print("  Moonraker: http://localhost:{}".format(paths.MOONRAKER_PORT))
        for line in _lan_urls():
            print(line)
        print("  Logs: klipperformac logs   Stop: klipperformac down")
        for line in prior:
            print(line)
        _power_warning()
        if not _wait_ready():
            sys.exit(1)
        if getattr(args, "open", False):
            webbrowser.open(paths.web_url())


def _wait_ready(timeout=35):
    url = "http://localhost:{}/server/info".format(paths.MOONRAKER_PORT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = (_moonraker_get("/server/info", 2) or {}).get("result")
        if isinstance(info, dict):
            state = info.get("klippy_state", "?")
            conn = "connected" if info.get("klippy_connected") else "not yet"
            print("  ready: moonraker up, klippy {} (state: {})".format(conn, state))
            return True
        time.sleep(0.7)
    print("[!] Moonraker did not answer within {}s — check: klipperformac logs"
          .format(timeout))
    held = [(port, pid) for port, pid in process.our_listeners()]
    squatters = process.port_conflicts()
    if squatters:
        for port, pid, desc in squatters:
            print("[!] Port {} is answered by {} (not this stack): stop that "
                  "program or run: klipperformac killall".format(
                      port, "pid {} ({})".format(pid, desc) if pid else desc))
    elif held:
        print("[!] Our own processes are alive on port(s) {} but nothing "
              "answered: run: klipperformac restart".format(
                  ", ".join(str(port) for port, _ in held)))
    return False


def _want_up(reason="up"):
    """Record "the user wants this stack running" for the login supervisor.

    Deliberately never fatal: run/ is the least important folder in this tool's
    life, and a command that already started the stack must not fail because a
    two-line intent file could not be written. What it does cost is stated:
    supervision cannot see the intent, so a crash is not repaired."""
    try:
        launchd.want_set(reason)
    except Exception as e:
        print("[!] autostart intent not recorded ({}): `klipperformac login` "
              "supervision cannot see that you want the stack up".format(e))


def _want_down(reason="down"):
    """Clear that intent, BEFORE the stack is stopped.

    This ordering is what keeps launchd from fighting a human: the supervisor
    takes the same cli lock we are holding, so it cannot sweep between the clear
    and the stop, and by the time it can, "down" is already on record."""
    try:
        launchd.want_clear(reason)
    except Exception as e:
        print("[!] autostart intent not cleared ({}): a running supervisor "
              "could bring the stack back".format(e))


def cmd_down(args):
    with _cli_lock():
        # The watchdog goes down BEFORE the stack: it pauses and cancels through
        # Moonraker's API, and a guard racing a dying Moonraker would only log a
        # failure it did not cause (and could cancel a print nobody asked about
        # on the way out). `stop()` is idempotent: off twice is still off.
        _want_down("down")
        guard_off, _guard_lines = batteryguard.stop()
        stopped = process.stop()
    cam_off, cam_lines = cam.stop()
    if guard_off:
        print("Battery guard stopped with the stack (still enabled: `up` "
              "starts it again; `batteryguard off` turns it off for good).")
    if cam_off:
        print("Stopped (camera turned off too)." if stopped
              else "Stack was not running; camera stopped.")
    elif stopped:
        print("Stopped.")
    else:
        print("Was not running.")
        for line in cam_lines or []:
            if "Camera was not on." not in line:
                print(line)
    # "Stopped." that leaves the API answering is worse than no answer at all:
    # with LAN on, the stack keeps serving the local network after the user said
    # stop. Report what is still there and how to clear it.
    left = process.our_listeners()
    if left:
        print("[!] Still listening: {} - clear with: klipperformac killall"
              .format(", ".join("port {} (pid {})".format(p, pid)
                                for p, pid in left)))
        sys.exit(1)


def cmd_killall(args):
    """Force-kill the whole stack plus any stray service processes that a
    plain `down` would leave behind (untracked proxies, orphan klipper/
    moonraker/caffeinate, and a battery guard whose ledger was lost)."""
    with _cli_lock():
        # Same rule as `down`: saying "kill everything" is the user's intent,
        # and a login supervisor that brought the stack back 10 s later would
        # turn a cleanup command into a fight.
        _want_down("killall")
        killed = process.kill_all() + cam.kill_orphans() \
            + batteryguard.kill_orphans()
    if not killed:
        print("Nothing to kill.")
        return
    print("Killed {} process(es):".format(len(killed)))
    for name, pid in killed:
        print("  {:<12} pid {}".format(name, pid))


def cmd_restart(args):
    with _cli_lock():
        batteryguard.stop()
        process.stop()
        _cmd_up(args)
        _guard_autostart()


def _status_powerloss_line():
    """The power-loss row of a RUNNING stack: is the byte offset of an active
    print being written down at all? The unclean-stop verdict is not printed here
    - with the stack up, the open session marker is that session, not evidence
    (powerloss.last_stop says why), and `up` already reported the session before
    this one."""
    cfg = powerloss.settings()
    if not cfg["journal"]:
        return ("  powerloss    journal off  (a power cut then leaves no seam: "
                "klipperformac powerloss on)")
    records = powerloss.read_records()
    last = records[-1] if records else None
    tail = "  last {} @ byte {}".format(last.get("filename"),
                                        last.get("file_position")) if last \
        else "  no snapshot yet"
    return "  powerloss    journal on  {} records{}".format(len(records), tail)


def _status_guard_lines():
    """The battery watchdog row for the stopped branch of `status`: printed
    when there is something to know - a live guard, or one the user enabled
    that is waiting for the stack. A guard nobody ever turned on stays silent:
    `status` on a machine with nothing installed is exactly one word
    ("stopped", pinned by tests/deep/test_clmisc_data_ui.py), and the camera
    analogy this row follows is the same - camera off is only ever listed in
    the running branch, where the user is reading a table of what is up."""
    guard = batteryguard.running()
    cfg = batteryguard.battery_settings()
    if guard is not None:
        return ["  {:<12} on  pid {}  pause {}%  critical {}%".format(
            "batteryguard", guard.get("pid"), cfg["pauseAt"],
            cfg["criticalAt"])]
    if cfg["enabled"]:
        return ["  {:<12} armed  pause {}%  critical {}% (starts with the "
                "stack)".format("batteryguard", cfg["pauseAt"],
                                cfg["criticalAt"])]
    return []


def cmd_status(args):
    state = process.running()
    if state is None:
        left = process.our_listeners()
        live_ports = {port for port, _pid in left}
        if _powerloss_session_live(state=None, live_ports=live_ports):
            print("running (untracked)")
        else:
            print("stopped")
        if left:
            print("  [!] {} still answering although no stack is tracked: "
                  "klipperformac killall".format(
                      ", ".join("port {}".format(p) for p, _ in left)))
        for line in _lan_urls(indent="  "):
            print(line)
        for line in _status_guard_lines():
            print(line)
        for line in powerloss.verdict_lines(
                stack_up=_powerloss_session_live(state=None,
                                                 live_ports=live_ports)):
            print(line)
        return
    pins = versions.load_pins()
    up = {n: st for n, st in state.items() if st}
    print("running" if up else "stopping...")
    for name in process.SERVICES:
        if name in ("caffeinate",):
            continue
        print("  {:<12} {}".format(name, "up" if state.get(name) else "DOWN"))
    klippy = _klippy_state() if up.get("moonraker") else None
    if up.get("moonraker"):
        print("  {:<12} {}".format(
            "klippy", klippy if klippy else "printer state unknown"))
    print("  url          {}".format(paths.web_url()))
    for line in _lan_urls():
        print(line)
    print("  pins         klipper {}  moonraker {}  mainsail {}  fluidd {}".format(
        pins["klipper"], pins["moonraker"], pins["mainsail"], pins["fluidd"]))
    st = shaper.readiness()
    print("  shaper       " + st["detail"])
    cam_state = cam.running()
    if cam_state:
        print("  camera       on  " + cam.stream_url(cam_state.get("port")))
    elif cam.helper_path():
        print("  camera       off  (turn on: klipperformac cam on)")
    else:
        print("  camera       not installed (build: klipperformac cam build)")
    # The battery guard is optional and toggled, so it is reported like the
    # camera - never as a service that looks DOWN whenever it is off.
    guard = batteryguard.running()
    guard_cfg = batteryguard.battery_settings()
    if guard is not None:
        print("  batteryguard on  pid {}  pause {}%  critical {}%".format(
            guard.get("pid"), guard_cfg["pauseAt"], guard_cfg["criticalAt"]))
    elif guard_cfg["enabled"]:
        print("  batteryguard armed  pause {}%  critical {}% (starts with the "
              "stack)".format(guard_cfg["pauseAt"], guard_cfg["criticalAt"]))
    else:
        print("  batteryguard off  (laptop prints: klipperformac "
              "batteryguard on)")
    print(_status_powerloss_line())
    updates, errors = versions.check_updates()
    for e in errors:
        print("  [!] " + e)
    if updates:
        print("  updates available: " + ", ".join(
            "{} {}->{}".format(c, cur, new) for c, cur, new in updates))


def cmd_lan(args):
    if args.state is not None:
        enabled = args.state == "on"
        settings = paths.load_settings()
        settings["lan"] = enabled
        _save_settings(settings)
        changed = configgen.apply_lan(enabled)
        print("LAN access {}.".format("OPEN — other devices can reach the "
                                      "printer" if enabled
                                      else "closed (this Mac only)"))
        if not paths.MOONRAKER_CONF.exists():
            print("[!] No moonraker.conf yet, so nothing was pinned in it: the "
                  "bind is written when the config is created (run: "
                  "klipperformac setup)")
        if process.running() is not None:
            # Both halves of the switch read their setting once, at startup:
            # Moonraker from moonraker.conf, our proxies from settings.json.
            # Saying "closed (this Mac only)." and leaving *:7125 answering is
            # the mistake this guards - a user who runs `lan off` before going
            # to a hotel network must be told the port is still open.
            print("[!] The stack is running: what is actually listening right "
                  "now is unchanged{}.".format(
                      "" if changed else " (the config already said this)"))
            print("Apply now with: klipperformac restart")
        elif not enabled:
            print("Next start stays localhost-only.")
    notes = _lan_urls(indent="")
    if paths.lan_enabled():
        urls = [line for line in notes if "[!]" not in line]
        if urls:
            print("Reachable from other devices on your network:")
            for line in urls:
                print("  " + line)
        for line in notes:
            if "[!]" in line:
                print(line)
    elif notes:
        for line in notes:
            print(line)
    else:
        print("Only this Mac can reach the stack (localhost). Turn on with: "
              "klipperformac lan on")


def cmd_logs(args):
    log = paths.KLIPPY_LOG if args.service == "klipper" else paths.MOONRAKER_LOG
    if not log.is_file():
        _die("No log yet: " + str(log))
    tail = ["tail", "-n", str(args.lines), str(log)]
    if args.follow:
        tail.insert(1, "-f")
    code = subprocess.call(tail)
    if code:
        # A failed `logs` must not look like a success to a script or to CI.
        sys.exit(1 if code < 0 else code)


def _identity_map():
    """{device_path: identity dict} for the USB boards attached right now, {} on
    any failure.

    detect.identity() is macOS' answer to Linux' /dev/serial/by-id: macOS
    renumbers /dev/cu.usbmodemNNNNN on every replug, but VID:PID + the USB
    serial number + locationID come from the IOKit registry and do not move.
    Nothing here opens a port (klippy holds its own with a flock), and one call
    runs one `ioreg`, so the whole listing costs ~0.5 s, not N * 0.5 s.
    """
    fn = getattr(detect, "all_identities", None)
    if not callable(fn):
        return {}
    try:
        rows = fn() or []
    except Exception:
        return {}
    return {r["path"]: r for r in rows
            if isinstance(r, dict) and r.get("path")}


def _identity_tail(ident, claim):
    """The extra columns of one `serial` row: VID:PID= SER= LOC= -> [mcu].

    A port with no identity behind it (a Bluetooth RFCOMM tunnel, a virtual
    serial, a Mac where ioreg was unreadable) prints exactly the old line -
    path plus driver hint - because `serial` output is parsed by scripts and
    the identity layer is an addition, never a requirement.
    """
    bits = []
    if ident:
        if ident.get("vid_pid"):
            bits.append("VID:PID=" + str(ident["vid_pid"]).lower())
        if ident.get("serial"):
            bits.append("SER=" + str(ident["serial"]))
        if ident.get("location"):
            bits.append("LOC=" + str(ident["location"]))
    if claim:
        bits.append("-> " + claim)
    return "  ".join(bits)


def _configured_mcus():
    """[(section, serial)] from printer.cfg, and {path: "[mcu]"} for them.

    flash.configured_serials()/claimed_by_section() own this parsing (they are
    section-aware and degrade gracefully), so `serial` and `flash detect` can
    never disagree about who claims which node - one read of printer.cfg, and
    nothing is ever written here.
    """
    try:
        from . import flash
        rows = flash.configured_serials()
        return rows, flash.claimed_by_section(rows)
    except Exception:
        return [], {}


def _serial_notes(ports, rows, claims):
    """Read-only advice about printer.cfg vs what is actually attached.

    Three facts that cost nothing to check and are worth real debugging time on
    macOS: a section whose `serial:` node is gone (the renumbering above), the
    one unclaimed node that is probably the renamed board, and two sections
    claiming the same node - which is not a warning but a dead printer, because
    klippy takes a port exclusively and the second board retries for 90 s and
    then errors out. Never writes anything: the fix stays the user's
    `serial --set`, typed by a human who can see the cable.
    """
    present = {p["path"] for p in ports}
    notes = []
    missing = 0
    owner = {}
    for name, serial in rows:
        label = "[mcu]" if name == "mcu" else "[mcu {}]".format(name)
        if serial in owner:
            notes.append("[!] {} and {} both point at {}: one klippy can hold "
                         "that port, the other retries for 90 s and then "
                         "errors.".format(owner[serial], label, serial))
        else:
            owner[serial] = label
        if serial not in present:
            missing += 1
            notes.append("[!] {} points at {}, which is not attached right now."
                         .format(label, serial))
    if missing and len(rows) == 1:
        free = [p["path"] for p in ports if p["path"] not in claims]
        if len(free) == 1:
            notes.append("    macOS may have renamed the node. If it is the "
                         "same board, re-point it: klipperformac serial --set "
                         + free[0])
    return notes


# ===========================================================================
# multi-MCU: EVERY [mcu*] section, plus one JSON contract
#
# Upstream already takes N MCUs: klippy/mcu.py:1246-1253 adds the object 'mcu'
# for the timing master and one object 'mcu <name>' (clocksync.SecondarySync)
# per '[mcu <name>]' section, which config.get_prefix_sections('mcu ') finds.
# What this tool assumed was exactly one: write_serial() only ever touched
# [mcu], `serial` listed ports without saying which section wanted which, and
# doctor asked klippy about "the printer" instead of about each MCU. A user with
# a toolboard MCU therefore had no way to see, set or diagnose cable 2.
#
# ONE CONTRACT (shared with the SwiftUI app / TUI consumer, pinned by
# tests/climcu/test_serial_json_contract.py for shape and by
# tests/contract/ for BYTES - tests/contract/serial_json.golden is this command's
# exact stdout for tests/contract/canon_multi_mcu.cfg, and the Swift decoder keeps
# the same bytes in its own source, so renaming a key here breaks a build there):
#
#   klipperformac serial --json  ->
#     {"mcus": [{"mcu": "mcu"|"tool", "section": "[mcu tool]",
#               "serial": "/dev/cu.usbmodem14101" | null,
#               "configured": true, "claimed": true,
#               "identity": {"vid_pid": "1d50:614e", "serial": "...",
#                            "key": "usb:...", "present": true} | null,
#               "conflict": null | "bad-section-name" | "duplicate-port"
#                         | "missing-port"}],
#      "single": true}
#
#   * mcus[] is in klipper's own priority: the timing master first, then the
#     secondary sections by name; `single` is len(mcus) <= 1 (a config with no
#     [mcu] at all is not a multi-MCU config, and the GUI must not branch on an
#     empty list).
#   * read-only mode always exits 0; a malformed printer.cfg shows up as a
#     `conflict` string on the entry, never as a traceback, because `serial
#     --json` is what a GUI calls to render a panel.
#   * `conflict` is RANKED, in the order of the tuple above: bad-section-name >
#     duplicate-port > missing-port. A section klipper would read as a different
#     object is the cause, and an unattached node it names is the symptom; a row
#     carrying both must be labelled with the one the user can only fix by hand,
#     or the GUI sends them to the cable. Both this file's own candidate and
#     mcus.validate()'s are ranked, never "whoever spoke first".
#   * `serial` is the value klipper would ACTUALLY use: a CAN section carries its
#     uuid here and `claimed` says it holds no USB node, so "not claimed" never
#     means "not configured".
#
# WHO OWNS WHAT: mcus.py (wave 3, agent A) owns the config reading/writing
# (list_mcus/validate/set_serial/add_mcu_section). This layer owns the CLI/JSON
# surface and the live USB + runtime facts (detect.scan, detect identity map,
# one Moonraker GET). Every call into mcus is defensive: when the module is not
# there, the same answers come from this file's own parse - a degrade path that
# mirrors klipper's name rules row for row, with the one remaining difference
# (raw control bytes inside a serial: value) named and explained in
# tests/contract/test_backend_agreement.py rather than papered over.
# ===========================================================================

MCU_IDENTITY_MARKER = "# klipperformac:identity"
MCU_PROBE_TIMEOUT = 3.0
_MCU_JSON_KEYS = ("mcu", "section", "serial", "configured", "claimed",
                  "identity", "conflict")
#: The contract's conflict vocabulary, IN PRECEDENCE ORDER: cause before symptom.
#: ``mcus.CONFLICT_CODES`` is the same tuple on the reader's side, and
#: ``tests/contract/test_json_bytes_contract.py`` pins the two against each other
#: (and against the Swift enum that turns them into badges), because `conflict` is
#: the one field a GUI renders without reading the config itself.
_MCU_CONFLICTS = ("bad-section-name", "duplicate-port", "missing-port")
_MCU_CONFLICT_RANK = {code: rank for rank, code in enumerate(_MCU_CONFLICTS)}
# /dev/cu.* / /dev/tty.* / the driver-named nodes: "a real port", the same
# test auto_serial has always used, now applied per section instead of to the
# first serial line anywhere in the file.
_MCU_REAL_PORT_RE = re.compile(r"/dev/(cu|tty|usbserial|usbmodem)\S*")
_MCU_COMMENT_RE = re.compile(r"[#;].*$")
_MCU_MARKER_RE = re.compile(
    r"[#;][^\n]*klipperformac:identity[ \t]*(\S+)", re.IGNORECASE)


def _mcus_module():
    """klipperformac.mcus when wave 3 has landed it, else None.

    Importing inside the function keeps `klipperformac status` from needing the
    module, and keeps a broken/absent mcus.py from ever being a traceback in a
    read-only command."""
    try:
        from . import mcus
    except Exception:
        return None
    return mcus


def _cfg_text(cfg_path):
    """The text of a config file, or "" when it cannot be read (missing, a
    directory, an unreadable encoding): every caller degrades to "no sections".
    """
    try:
        return paths.read_config_text(cfg_path)
    except Exception:
        return ""


def _real_serial(value):
    """True when a section's serial: already names a real device node."""
    return bool(value) and bool(_MCU_REAL_PORT_RE.search(str(value)))


def _mcu_marker_key(line):
    """The '# klipperformac:identity <key>' binding inside a section, or None.

    The key is detect.identity()['key'] ('usb:VID:PID:SERIAL'): macOS
    renumbers /dev/cu.usbmodemNNNNN on every replug, so a section bound by node
    path breaks and a section bound by USB identity does not. configgen.py does
    NOT write this marker (untouched on purpose), so its absence is the normal
    case and every reader here treats a section without it as "guess nothing".
    """
    m = _MCU_MARKER_RE.search(line)
    return m.group(1) if m else None


def _mcu_row_name(row):
    """The contract's "mcu" field for one row - and never an invented name.

    ``row.get("name") or "mcu"`` reads as harmless defaulting and is not:
    '[mcu ]' is a real section whose klipper-derived name is the EMPTY string
    (klippy/mcu.py:1151-1158 strips exactly the four characters 'mcu ' from
    'mcu ', and nothing is left), and a present-but-falsy name replaced by
    "mcu" points the GUI's per-section write - and its row label - straight at
    the timing master's own section. So presence of the key decides, not its
    truthiness; only a row that carries no name at all falls back.
    """
    for key in ("mcu", "name"):
        if key in row and row[key] is not None:
            return str(row[key])
    return "mcu"


def _mcu_bad_name(name, is_master=False):
    """True when '[mcu <name>]' is not something this tool can write back
    verbatim - klipper's object name would not be the name the user typed.

    Mirrors ``mcus.name_problem()`` (the reader's own rule) so a section is
    called out the same way whether mcus.py answered or this file's parse did:
    klipper strips four characters and registers the RESULT as the pin chip
    (klippy/pins.py:126-131), so an empty, padded or multi-word name is a
    different board, not a prettier spelling of the same one. `detect.mcu_header`
    covers the character set (no ':' ';' '#' '[' ']' whitespace, one word).
    """
    if is_master:
        return False
    if not name or name != name.strip() or any(c.isspace() for c in name):
        return True
    return not detect.mcu_header(name)


def _mcu_section_name(inner, closed=True):
    """``(is_mcu, mcu_name, bad_name)`` for a header's inner text, klipper's way.

    `inner` is the text between the FIRST '[' and the LAST ']' of the line as
    klipper reads it (configparser's SECTCRE, greedy). The three outcomes match
    ``mcus._entry()`` deliberately:
      * 'mcu' -> the timing master, name 'mcu', never badly named;
      * 'mcu ' + anything -> a SecondarySync board (klippy/mcu.py:1247), named
        by what is LEFT of the four stripped characters, spaces included;
      * anything else that merely looks like an mcu ('MCU', 'Mcu tool',
        'mcu] [mcu tool', an unterminated '[mcu tool') -> klipper builds no MCU
        object and then stops on the section (configfile.py:430-441), so it is
        reported under the name the file uses and marked bad.
    """
    lowered = inner.lower()
    is_mcu = closed and (inner == "mcu" or inner.startswith("mcu "))
    if not (lowered == "mcu" or lowered.startswith("mcu ")
            or ("mcu" in lowered and "]" in inner)):
        # Not an mcu section and does not look like one: not this command's
        # business ('[stepper_x]', '[fan]', a plain typo in another section).
        return False, None, False
    if is_mcu and inner == "mcu":
        return True, "mcu", False
    if is_mcu:
        name = inner[4:]
        return True, name, _mcu_bad_name(name)
    return False, inner, True


def _parse_mcu_sections(text):
    """[{mcu, section, serial, marker, bad_name}] for each [mcu*] header.

    Never raises and reads the way klippy reads (klippy/configfile.py: a line
    whose comment-stripped text starts with '[' is a header, the section name
    runs to the LAST ']' on that line, and a repeated header continues the same
    section - so a config that includes the same [mcu tool] twice does not
    become two MCUs here). The marker comment is read from the RAW line, because
    a comment is exactly where it lives.

    This is the degrade path: `mcus.list_mcus()` is the reader of record and this
    parse mirrors its name rules exactly for every header that is one well-formed
    line ('[mcu  a]' -> ' a', '[mcu ]' -> '', '[mcu a ]' -> 'a ', '[MCU]' -> its
    own bad-section row). The residual differences are listed, labelled and
    explained in tests/contract/test_backend_agreement.py - they are all shapes
    only the klipper-faithful reader can describe at all.
    """
    rows = []
    by_name = {}
    current = None
    serial_row = None       # section whose serial: the LAST option line set
    for raw in (text or "").splitlines():
        pruned = _MCU_COMMENT_RE.sub("", raw).strip()
        if pruned.startswith("["):
            closed = pruned.endswith("]")
            inner = pruned[1:-1] if closed else pruned[1:]
            is_mcu, name, bad = _mcu_section_name(inner, closed)
            if name is None:
                current = None
                serial_row = None
                continue
            current = by_name.get(inner)
            if current is None:
                current = {"mcu": name, "section": "[" + inner + "]",
                           "serial": None, "marker": None,
                           "canbus": None, "is_mcu": is_mcu,
                           "bad_name": bad}
                by_name[inner] = current
                rows.append(current)
            serial_row = None
            continue
        if current is None:
            continue
        if not pruned:
            # A blank or comment line: neither ends an option's value for klipper
            # (configfile.py counts it and keeps joining), and it is exactly where
            # a '# klipperformac:identity' binding lives, so fall through to the
            # marker read below without touching serial_row.
            pass
        elif raw[:1].isspace() and serial_row is not None:
            # An indented line CONTINUES the option above it and klipper joins
            # it into the value (configfile.py:172), so 'serial:' now holds two
            # lines and names no device node at all. mcus.list_mcus() reports
            # that as "no usable serial"; this parse has to say the same, or
            # auto_serial() calls a two-line value a port and reports a printer
            # that cannot start as configured.
            serial_row["serial"] = None
            serial_row = None
            continue
        else:
            serial_row = None
            m = re.match(r"serial[ \t]*:[ \t]*(\S+)", pruned)
            if m:
                current["serial"] = m.group(1)   # klipper: last one wins
                serial_row = current
                continue
            can = re.match(r"canbus_uuid[ \t]*:[ \t]*(\S+)", pruned)
            if can:
                # A CAN MCU is configured and complete and is NOT a /dev/* node:
                # auto_serial() must never overwrite it with a USB port, and
                # "not attached" cannot be checked from here.
                current["canbus"] = can.group(1)
        marker = _mcu_marker_key(raw)
        if marker and current["marker"] is None:
            current["marker"] = marker
    return rows


def _mcu_key_parts(key):
    """('1d50:614e', '5044506110A45B1C') from 'usb:1d50:614e:<serial>'.

    None, None for anything that is not that shape - a marker a human typed
    wrong must degrade to 'no identity', never to a guessed board."""
    parts = str(key or "").split(":")
    if len(parts) < 4 or parts[0].lower() != "usb":
        return None, None
    vid, pid = parts[1], parts[2]
    if len(vid) != 4 or len(pid) != 4:
        return None, None
    try:
        int(vid, 16)
        int(pid, 16)
    except ValueError:
        return None, None
    return "{}:{}".format(vid.lower(), pid.lower()), ":".join(parts[3:])


def _mcu_identity_view(ident, present):
    """The contract's identity object, projected from a detect.identity() dict.

    Four keys, always the same four: the identity dict is additive on purpose
    (ioservice_path, name, vendor, location...), and a GUI parsing this contract
    must never be surprised by a key it did not ask for.
    """
    if not ident:
        return None
    return {"vid_pid": (str(ident.get("vid_pid")).lower()
                        if ident.get("vid_pid") else None),
            "serial": (str(ident.get("serial"))
                       if ident.get("serial") else None),
            "key": (str(ident.get("key")) if ident.get("key") else None),
            "present": bool(present)}


def _mcu_rows(cfg_path, text):
    """Section rows: mcus.list_mcus(text) when wave 3 has it, else our own parse.

    Both are projected to the same facts this layer needs (name, section header,
    serial, canbus, marker, bad name, conflict), so the live computation has one
    input shape. mcus.py owns reading printer.cfg the way klipper reads it
    (configparser's own SECTCRE, last-value-wins, comment rules) - it does NOT
    know about the '# klipperformac:identity' binding comment, so that still
    comes from this file's parse and is merged in by name."""
    mod = _mcus_module()
    list_fn = getattr(mod, "list_mcus", None) if mod else None
    if not callable(list_fn):
        return None
    try:
        entries = list_fn(text)
    except Exception:
        return None
    if not isinstance(entries, (list, tuple)):
        return None
    conflict_fn = getattr(mod, "conflicts_by_mcus", None)
    conflicts = {}
    if callable(conflict_fn):
        try:
            got = conflict_fn(entries)
            if isinstance(got, dict):
                conflicts = got
        except Exception:
            conflicts = {}
    markers = {}
    for row in _parse_mcu_sections(text):
        if row.get("marker"):
            markers[row["mcu"]] = row["marker"]
    out = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        name = _mcu_row_name(row)
        header = row.get("section_header") or detect.mcu_header(name) \
            or "[mcu ?]"
        # mcus.py owns the name rules (four-character strip, pin-chip collisions),
        # so take its verdict when it gives one; the recomputation below is only
        # for a module that answers without those keys.
        if "bad_name" in row:
            bad = bool(row["bad_name"])
        else:
            bad = bool(row.get("malformed_header"))
            if "is_mcu" in row:
                bad = bad or not row.get("is_mcu")
            else:
                bad = bad or _mcu_bad_name(name, name == "mcu")
        out.append({"mcu": name,
                    "section": header,
                    "serial": row.get("serial") or None,
                    "canbus": row.get("canbus_uuid") or row.get("canbus"),
                    "marker": row.get("marker") or markers.get(name),
                    "bad_name": bad,
                    "configured": row.get("configured", True),
                    "conflict": conflicts.get(name)})
    return out


def _mcu_entries(cfg_path=None, text=None, ports=None, idents=None):
    """Contract-shaped entries for every [mcu*] section, live facts layered on.

    One read of printer.cfg, at most one ioreg, zero writes. `conflict`
    precedence is structural first (a section klipper would not read as its own
    name), then duplicate-port (two sections, one port: the dead-printer case),
    then missing-port (no serial line, or a node that is not attached right
    now). A CAN section is configured and claims no USB node, so it is never
    reported as missing.
    """
    cfg_path = cfg_path or paths.PRINTER_CFG
    if text is None:
        text = _cfg_text(cfg_path)
    rows = _mcu_rows(cfg_path, text)
    if rows is None:
        rows = _parse_mcu_sections(text)
    if ports is None:
        try:
            ports = detect.scan()
        except Exception:
            ports = []
    if idents is None:
        idents = {}
    present = {p.get("path") for p in ports if p.get("path")}
    by_key = {}
    for ident in (idents or {}).values():
        key = str(ident.get("key") or "").lower()
        if key:
            by_key.setdefault(key, ident)
    entries = []
    for row in rows:
        name = _mcu_row_name(row)
        header = row.get("section") or detect.mcu_header(name) or "[mcu ?]"
        if row.get("configured", True) is False:
            # A row mcus offered for a section that is not in the file: nothing
            # to conflict about, and never a port to write.
            entries.append({"mcu": name, "section": header, "serial": None,
                            "configured": False, "claimed": False,
                            "canbus": None, "marker": None, "identity": None,
                            "conflict": None})
            continue
        serial = row.get("serial") or None
        canbus = row.get("canbus") or None
        entries.append({"mcu": name, "section": header,
                        "serial": serial or canbus,
                        "configured": True,
                        # `claimed` is about a USB node: a CAN uuid configures
                        # an MCU but holds no /dev/cu.* open.
                        "claimed": bool(serial),
                        "canbus": canbus,
                        "marker": row.get("marker"),
                        "_bad_name": bool(row.get("bad_name")),
                        "_conflict": row.get("conflict"),
                        "identity": None})
    owners = {}
    for e in entries:
        if e["claimed"]:
            owners.setdefault(e["serial"], []).append(e["section"])
    for e in entries:
        ours = None
        if e.get("_bad_name"):
            ours = "bad-section-name"
        elif e["claimed"] and len(owners.get(e["serial"], [])) > 1:
            ours = "duplicate-port"
        elif e["configured"] and not e.get("canbus") and \
                (not e["claimed"] or e["serial"] not in present):
            ours = "missing-port"
        given = e.get("_conflict")
        given = given if given in _MCU_CONFLICT_RANK else None
        # RANK the candidates, never "whoever spoke first". '[mcu  a]' with a
        # serial: is both badly named (what mcus.py, reading klipper's own rules,
        # reports) and holding a node that is not attached right now (what this
        # layer can see live). Reporting the second sends the user to the cable
        # when the header is the bug - and the header is the one thing a GUI
        # cannot fix for them.
        ranked = [code for code in (ours, given) if code]
        e["conflict"] = _mcu_conflict_pick(ranked)
        e["identity"] = _mcu_identity(e, present, idents, by_key)
    entries.sort(key=lambda e: (e["mcu"] != "mcu", e["mcu"]))
    return entries


def _mcu_conflict_pick(codes):
    """The highest-precedence conflict of several, or None."""
    if not codes:
        return None
    return min(codes, key=_MCU_CONFLICT_RANK.__getitem__)


def _mcu_identity(entry, present, idents, by_key):
    """The identity behind one section: the node it names, or the board its
    marker binds, or None. `present` is a live fact, never a hope."""
    serial = entry.get("serial")
    if serial and idents.get(serial):
        return _mcu_identity_view(idents[serial], True)
    key = str(entry.get("marker") or "").lower()
    if key:
        hit = by_key.get(key)
        if hit:
            return _mcu_identity_view(hit, hit.get("path") in present)
        vid_pid, serial_no = _mcu_key_parts(entry.get("marker"))
        return {"vid_pid": vid_pid, "serial": serial_no or None,
                "key": entry.get("marker"), "present": False}
    if serial:
        # A node that exists but has no USB board behind it (a virtual port, a
        # Bluetooth tunnel, an unreadable ioreg) is still not an identity.
        return None
    return None


def _mcu_json(entries):
    """The --json payload: exactly the contract, keys in contract order."""
    mcus = []
    for e in entries:
        mcus.append({"mcu": e["mcu"], "section": e["section"],
                     "serial": e.get("serial"),
                     "configured": bool(e.get("configured", True)),
                     "claimed": bool(e.get("claimed")),
                     "identity": e.get("identity"),
                     "conflict": e.get("conflict")})
    return {"mcus": mcus, "single": len(mcus) <= 1}


def _mcu_json_line(entries):
    """The contract as one stdout line. Never a traceback and never a partial
    document: a GUI/TUI caller parses stdout, so anything that cannot be said
    as a contract entry is said on stderr, and the shape stays valid."""
    try:
        return json.dumps(_mcu_json(entries), default=str)
    except Exception as e:
        print("[!] serial --json could not describe the sections: {}".format(e),
              file=sys.stderr)
        return json.dumps({"mcus": [], "single": True})


def _mcu_table_lines(entries, ports):
    """The per-[mcu]-section table `serial` prints under the port listing.

    Lines start with two spaces and a section name, never with a device path:
    scripts (and the SwiftUI picker) read the port rows by their leading
    /dev/cu.*, and that contract is older than this table.
    """
    present = {p.get("path") for p in ports}
    lines = ["", "MCU sections in {}:".format(paths.PRINTER_CFG)]
    for e in entries:
        ident = e.get("identity") or {}
        serial = e.get("serial")
        if not e.get("configured", True):
            verdict = "not in printer.cfg"
        elif e.get("conflict") == "bad-section-name":
            verdict = "bad section name (klippy reads a different object)"
        elif e.get("conflict") == "duplicate-port":
            verdict = "CONFLICT 2 sections on one port"
        elif not serial:
            verdict = "no serial set"
        elif serial in present:
            verdict = "attached"
        else:
            verdict = "port not attached"
        tail = ""
        if ident.get("key"):
            tail = "  identity " + str(ident["key"])
            if ident.get("present") is False:
                tail += " (not attached)"
        lines.append("  {:<14} {:<30} {}{}".format(
            e["section"], str(serial or "-"), verdict, tail))
    lines.append("  (machine-readable: klipperformac serial --json)")
    return lines


def _mcu_section_serial(cfg_path, name):
    """The serial: a section holds in printer.cfg RIGHT NOW, or None.

    Read with the same reader the listing uses, so a write can be verified
    against the file instead of against a return value."""
    text = _cfg_text(cfg_path)
    rows = _mcu_rows(cfg_path, text)
    if rows is None:
        rows = _parse_mcu_sections(text)
    for row in rows:
        if row.get("mcu") == name:
            return row.get("serial")
    return None


def _write_mcu_serial(cfg_path, device_path, name="mcu"):
    """Point ONE [mcu*] section at a port: (ok, message|None).

    mcus.set_serial() (text in / text out, klipper's own section rules, and a
    refusal that names the section it refused) is the writer when wave 3 has it;
    this command owns the file, so the read/write pair stays the byte-safe
    atomic paths.read_config + paths.write_config. Without the module,
    detect.write_serial(cfg, path, name) - the same kernel, one section.

    Either way `ok` is verified by reading the section back: an exit 0 that wrote
    nothing is a lie CI, scripts and the GUI all believe (AGENTS.md 4).
    """
    mod = _mcus_module()
    fn = getattr(mod, "set_serial", None) if mod else None
    if callable(fn):
        try:
            text, encoding = paths.read_config(cfg_path)
        except Exception as e:
            return False, "could not read {}: {}".format(
                cfg_path, getattr(e, "strerror", None) or e)
        updated = None
        try:
            updated = fn(text, name, device_path)
        except TypeError:
            updated = None          # unknown signature: use our own writer
        except Exception as e:
            return False, str(e) or "mcus.set_serial() refused the write"
        if isinstance(updated, str):
            try:
                paths.write_config(cfg_path, updated, encoding)
            except Exception as e:
                return False, "could not write {}: {}".format(
                    cfg_path, getattr(e, "strerror", None) or e)
            return _mcu_section_serial(cfg_path, name) == device_path, None
    try:
        if not detect.write_serial(cfg_path, device_path, name):
            return False, None
    except Exception as e:
        return False, str(e)
    return _mcu_section_serial(cfg_path, name) == device_path, None


def _mcu_conflicts(cfg_path):
    """Human-readable problems for one config file (used by `presets --import`).

    mcus.validate() first - it knows klipper's rules and explains each one; with
    no module, the same conflicts `serial --json` reports, minus "not attached
    right now", which is not a defect in a preset (the printer may be unplugged,
    or on another Mac). Never raises and never blocks: an import still imports.
    """
    warnings = _mcu_validate(cfg_path)
    if warnings is not None:
        return warnings
    out = []
    for e in _mcu_entries(cfg_path=cfg_path, ports=[], idents={}):
        conflict = e.get("conflict")
        if conflict == "missing-port" and e.get("serial"):
            continue
        if conflict == "missing-port":
            out.append("{} has no serial: (klipper cannot start it)".format(
                e["section"]))
        elif conflict:
            out.append("{}: {}".format(e["section"], conflict))
    return out


def _mcu_validate(cfg_path):
    """mcus.validate() normalised to [str] messages; None when wave 3 has not
    landed (the caller then falls back to its own conflict parse)."""
    mod = _mcus_module()
    validate_fn = getattr(mod, "validate", None) if mod else None
    list_fn = getattr(mod, "list_mcus", None) if mod else None
    if not (callable(validate_fn) and callable(list_fn)):
        return None
    try:
        issues = validate_fn(list_fn(_cfg_text(cfg_path)))
    except Exception:
        return None
    msgs = []
    for issue in issues if isinstance(issues, (list, tuple)) else []:
        if isinstance(issue, dict):
            msg = issue.get("message")
            if msg:
                msgs.append(str(msg))
        elif isinstance(issue, str) and issue:
            msgs.append(issue)
    return msgs


def _mcu_object_name(name):
    """klippy's object name for a section: 'mcu', or 'mcu <name>' (the
    SecondarySync one) - klippy/mcu.py:1246-1253."""
    return "mcu" if name == "mcu" else "mcu {}".format(name)


def _mcu_runtime(name, timeout=MCU_PROBE_TIMEOUT):
    """('ok'|'missing'|'unknown', detail) for one MCU, from one read-only GET.

    /printer/objects/query?mcu%20<name>=mcu_version is how Mainsail asks the
    same question. Moonraker answers 200 with a null value for an object klippy
    never loaded, so `null` here really does mean "klippy has no such MCU" -
    and no answer at all (stack down, klippy in shutdown, a 3 s timeout) is
    'unknown', which no caller may treat as a failure.
    """
    obj = _mcu_object_name(name)
    data = _moonraker_get("/printer/objects/query?{}=mcu_version".format(
        obj.replace(" ", "%20")), timeout=timeout)
    if not isinstance(data, dict):
        return "unknown", "moonraker did not answer"
    status = (data.get("result") or {}).get("status") or {}
    if obj not in status:
        return "unknown", "no {} object in the answer".format(obj)
    version = (status.get(obj) or {}).get("mcu_version")
    if version:
        return "ok", str(version)
    return "missing", "klippy has no firmware version for it (not loaded)"


def _klipper_checkout_version(timeout=3.0):
    """The installed Klipper checkout's short git revision, or None.

    Read-only and cheap: one bounded `git rev-parse --short=7 HEAD` against the
    pristine checkout this install is already using. A detached HEAD is fine;
    an unreadable/non-git tree just means this extra consistency check is
    skipped, never that doctor dies."""
    if not paths.KLIPPER.is_dir():
        return None
    try:
        res = subprocess.run(
            ["git", "-C", str(paths.KLIPPER), "rev-parse", "--short=7", "HEAD"],
            capture_output=True, timeout=timeout)
    except Exception:
        return None
    if res.returncode != 0:
        return None
    text = res.stdout.decode(errors="replace").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{7,40}", text) else None


def _version_hex_candidates(text):
    """Git-ish hex tokens found in a version string, normalised to lower-case."""
    text = str(text or "").lower()
    hits = set(re.findall(r"\b[0-9a-f]{7,40}\b", text))
    hits.update(re.findall(r"-g([0-9a-f]{7,40})\b", text))
    return sorted(hits)


def _runtime_matches_checkout(runtime, expected):
    """Whether an MCU version string names the installed Klipper revision."""
    expected = str(expected or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", expected):
        return False
    for token in _version_hex_candidates(runtime):
        if token.startswith(expected) or expected.startswith(token):
            return True
    return False


def _mcu_runtime_rows(cfg_path=None, entries=None):
    """Per-configured-MCU runtime rows shared by doctor/selftest helpers."""
    entries = _mcu_entries(cfg_path=cfg_path) if entries is None else entries
    rows = []
    for e in entries:
        if not e.get("configured", True):
            continue
        verdict, detail = _mcu_runtime(e["mcu"])
        rows.append({
            "entry": e,
            "section": e["section"],
            "mcu": e["mcu"],
            "serial": e.get("serial"),
            "verdict": verdict,
            "detail": detail,
        })
    return rows


def _mcu_lines_fail(lines, klippy):
    """Whether any per-MCU line should turn `doctor` red.

    Only one verdict is a failure, and only in one state: a section klippy did
    not load while klippy claims `ready` (so the section is missing from a
    printer that otherwise works). 'unknown' - stack down, klippy in shutdown,
    the 3 s GET with no answer - is never an accusation: doctor's exit code is
    the install-correctness gate AGENTS.md 4 puts on "done", not a hardware
    attendance sheet."""
    return any(verdict == "missing" and klippy == "ready"
               for _tag, _text, verdict in (lines or []))


def _mcu_firmware_lines_fail(lines, klippy):
    """Whether a firmware-vs-checkout mismatch should turn doctor red."""
    return any(verdict == "mismatch" and klippy == "ready"
               for _tag, _text, verdict in (lines or []))


def _mcu_report_lines(cfg_path=None, entries=None, rows=None):
    """[(tag, text)] one line per configured MCU, shared by doctor and selftest
    so the two can never disagree about the same printer."""
    rows = _mcu_runtime_rows(cfg_path=cfg_path, entries=entries) \
        if rows is None else rows
    lines = []
    for row in rows:
        e = row["entry"]
        verdict = row["verdict"]
        detail = row["detail"]
        if verdict == "ok":
            tag = "[ok] "
            text = "mcu {}: loaded by klippy (firmware {})".format(
                e["section"], detail)
            if e.get("serial"):
                text += " on {}".format(e["serial"])
        elif verdict == "missing":
            tag = "[!] "
            text = "mcu {}: klippy did not load this section ({})".format(
                e["section"], detail)
        else:
            tag = "[i]  "
            text = "mcu {}: runtime unknown ({})".format(e["section"], detail)
        lines.append((tag, text, verdict))
    return lines


def _mcu_firmware_match_lines(rows=None, expected=None):
    """Extra lines that compare loaded MCU firmware to the installed checkout."""
    rows = [] if rows is None else rows
    expected = _klipper_checkout_version() if expected is None else expected
    if not expected:
        return []
    lines = []
    for row in rows:
        if row["verdict"] != "ok":
            continue
        if _runtime_matches_checkout(row["detail"], expected):
            lines.append((
                "[ok] ",
                "mcu {}: firmware matches installed klipper {}".format(
                    row["section"], expected),
                "match",
            ))
        else:
            lines.append((
                "[!] ",
                "mcu {}: firmware {} does not match installed klipper {}".format(
                    row["section"], row["detail"], expected),
                "mismatch",
            ))
    return lines


def _mcu_log_provenance_lines(rows=None, log_info=None):
    """Extra lines from klippy.log's latest Loaded-MCU records."""
    rows = [] if rows is None else rows
    log_info = _mcu_log_provenance() if log_info is None else log_info
    lines = []
    for row in rows:
        if row["verdict"] != "ok":
            continue
        info = log_info.get(row["mcu"])
        if not info:
            continue
        if info["version"] == row["detail"]:
            lines.append((
                "[ok] ",
                "mcu {}: klippy.log last loaded {} ({})".format(
                    row["section"], info["version"], info["build_versions"]),
                "match",
            ))
        else:
            lines.append((
                "[!] ",
                "mcu {}: klippy.log last loaded {}, but runtime says {}".format(
                    row["section"], info["version"], row["detail"]),
                "mismatch",
            ))
    return lines


def _mcu_log_lines_fail(lines, klippy):
    """Whether a klippy.log/runtime mismatch should turn doctor red."""
    return any(verdict == "mismatch" and klippy == "ready"
               for _tag, _text, verdict in (lines or []))


def cmd_serial(args):
    """`serial` / `serial --json` / `serial --set PATH [MCUNAME]`.

    Three layers from one read of printer.cfg: the device nodes macOS offers,
    the [mcu*] SECTIONS that want them (upstream takes N of them - see the
    multi-MCU block above), and the advice that only appears when the two are
    put side by side. --json is the same information as the contract: always
    exit 0 in read-only mode, never a traceback, because a GUI panel that
    cannot parse a config is a red box, not a crash.
    """
    as_json = bool(getattr(args, "json", False))
    # "not given" and "given as the empty string" are different requests: an
    # empty MCUNAME means '[mcu ]' (a section klipper names ''), and defaulting
    # it to the timing master would write the wrong board's port on a typed
    # argument. See _mcu_row_name().
    raw_name = getattr(args, "mcu", None)
    name = "mcu" if raw_name is None else str(raw_name).strip()
    ports = detect.scan()
    idents = _identity_map() if ports else {}
    rows, claims = _configured_mcus()
    try:
        entries = _mcu_entries(ports=ports, idents=idents)
    except Exception as e:
        # Read-only commands do not traceback: say what could not be read and
        # carry on with the port listing, which needs neither mcus nor the cfg.
        print("[!] mcu sections were not read: {}".format(e), file=sys.stderr)
        entries = []
    target = getattr(args, "set", None)
    if not as_json:
        # --json means stdout is exactly one JSON document: no port rows, no
        # advice, no "Wrote serial to". Diagnostics go to stderr instead.
        for p in ports:
            desc = (" (" + p["description"] + ")") if p["description"] else ""
            tail = _identity_tail(idents.get(p["path"]), claims.get(p["path"]))
            print(p["path"] + desc + ("  " + tail if tail else ""))
    if args.auto and not target:
        if len(ports) == 1:
            target = ports[0]["path"]
        elif not ports:
            _die("No USB printer board detected, so --auto has nothing to "
                 "pick. Plug it in and retry, or name the port: "
                 "klipperformac serial --set /dev/cu.usbmodem14101")
        else:
            _die("--auto: {} candidates found; pick one of the above: "
                 "klipperformac serial --set PATH".format(len(ports)))
    if not target:
        # Read-only: the listing (or the contract) is the whole answer, and a
        # malformed printer.cfg is a `conflict` on a row, never an exit 1.
        if as_json:
            print(_mcu_json_line(entries))
            return
        if not ports:
            print("No USB serial devices found. Connect the printer board and "
                  "retry.")
        if entries:
            for line in _mcu_table_lines(entries, ports):
                print(line)
        for note in _serial_notes(ports, rows, claims):
            print(note)
        if ports:
            print("\nTo use one: klipperformac serial --set "
                  "/dev/cu.usbserial-XXXX [MCUNAME] (or --auto for the first "
                  "above)")
        return
    # From here the user asked for a specific port: an exit 0 that wrote
    # nothing (no device scanned, no printer.cfg, no [mcu]) is a lie that CI,
    # scripts and the GUI all believe. Every failure path below is a real error.
    if not paths.PRINTER_CFG.exists():
        _die("No printer.cfg yet. Run: klipperformac setup")
    if re.search(r"[\r\n\0]", target):
        _die("Invalid device path: must not contain newlines")
    header = detect.mcu_header(name)
    if header is None:
        _die("Invalid MCU name '{}': one word, no spaces, no ':' or ']' - "
             "klipper would read a section back as a different object than the "
             "name you typed.".format(name))
    sections = _mcu_entries(cfg_path=paths.PRINTER_CFG, ports=ports,
                            idents=idents)
    names = [e["mcu"] for e in sections if e.get("configured", True)]
    if name not in names:
        _die("No {} section in {}: nothing was written. Available: {} "
             "(klipperformac serial --json gives every section with its port)."
             .format(header, paths.PRINTER_CFG,
                     ", ".join(e["section"] for e in sections
                               if e.get("configured", True))
                     or "no MCU section at all"))
    target_section = [e for e in sections
                      if e["mcu"] == name and e.get("configured", True)]
    if target_section and target_section[0].get("canbus"):
        # mcu.py:780-791 - `canbus_uuid` WINS and the serial line is never even
        # read, so writing one into a CAN section is a config edit that does
        # nothing and looks like it did something. AGENTS.md rule 2: never change
        # printer.cfg semantics silently.
        _die("{} connects over CAN (canbus_uuid: {}): a serial: line there is "
             "read by nothing (klippy/mcu.py:780-791). Nothing was written to "
             "{}.".format(header, target_section[0]["canbus"], header))
    holders = {}
    for e in sections:
        if e.get("claimed") and e.get("serial"):
            holders.setdefault(e["serial"], (e["mcu"], e["section"]))
    held = holders.get(target)
    if held and held[0] != name:
        # Two sections on one node is not advice, it is a dead printer: klippy
        # holds a port exclusively and the second board retries for 90 s and
        # then errors. Refusing the write is the only honest answer here.
        _die("{} is already claimed by {} in printer.cfg: refusing to point "
             "{} at the same port. One klippy can hold it, the other retries "
             "for 90 s and then errors. Give {} its own port.".format(
                 target, held[1], header, header))
    if not os.path.exists(target):
        _die("{} is not a device that exists right now: nothing was written "
             "to {}".format(target, paths.PRINTER_CFG))
    wrote, reason = _write_mcu_serial(paths.PRINTER_CFG, target, name)
    if wrote:
        if target not in [p["path"] for p in ports]:
            note = ("[!] {} was not on the scanned list; a device node that "
                    "exists is accepted anyway (the scanner only knows the "
                    "common USB-serial name families)".format(target))
            print(note, file=sys.stderr if as_json else sys.stdout)
        if as_json:
            print(_mcu_json_line(_mcu_entries(
                cfg_path=paths.PRINTER_CFG, ports=ports, idents=idents)))
        else:
            print("Wrote serial to {} for {}".format(paths.PRINTER_CFG, header))
    else:
        _die("Could not write the serial: line for {} in {}: {}".format(
            header, paths.PRINTER_CFG,
            reason or "no such section, or the file refused it"))


def relocate_data(new):
    """Move the data folder, record it in settings, fix config paths.

    Everything is validated BEFORE the first file moves: a half-moved tree
    would hide printer.cfg from every later command."""
    typed = str(new)
    new = Path(new).expanduser()
    if not typed.strip():
        return ("Refusing to use a blank name as the data folder. Give a full "
                "path, e.g. /Volumes/BigDrive/klipper-data.")
    if not new.is_absolute():
        return ("Refusing to use the relative path '{}' as the data folder: "
                "the stack is started from a different folder than this "
                "terminal, so give a full path (starting with / or ~)."
                .format(typed))
    if paths.path_exists(new):
        new = new.resolve()
    old = paths.DATA
    resolved_old = old.resolve() if paths.path_exists(old) else old
    if new == resolved_old:
        return "Data folder already set to: {}".format(new)
    if new == Path.home() or new.parent == new:
        return "Refusing to use {} as the data folder.".format(new)
    if paths.protected_data_dir(new):
        return ("Refusing to use {} as the data folder: that is one of your "
                "own document folders (or your home). `klipperformac "
                "uninstall --data` deletes the data folder, and a top-level "
                "folder holds far more than the printer. Give a folder of its "
                "own, e.g. {}").format(new, paths.DEFAULT_DATA / "data")
    if paths.is_dir(new):
        try:
            entries = [p for p in new.iterdir() if not p.name.startswith(".")]
        except OSError as e:
            _die("Invalid data folder {}: {}".format(
                new, getattr(e, "strerror", None) or e))
        if entries and not paths.looks_like_data_dir(new):
            return ("Refusing to move the printer data into {}: it already "
                    "holds {} item(s) and does not look like a Klipper-for-Mac "
                    "data folder (no config/printer.cfg, no run/logs/gcodes). "
                    "Create an empty folder for it first - `uninstall --data` "
                    "would otherwise delete somebody else's files"
                    .format(new, len(entries)))
        if entries:
            print("[!] {}: already holds a Klipper-for-Mac data folder; "
                  "merging into it".format(new))
    if os.path.realpath(str(new)).startswith(
            os.path.realpath(str(old)) + os.sep):
        _die("Invalid data folder {}: it is inside the current data folder "
             "{}. Move the folder you want to use out of it first."
             .format(new, old))
    try:
        new.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as e:
        _die("Invalid data folder {}: {}".format(
            new, getattr(e, "strerror", None) or e))
    was_running = process.running() is not None
    if was_running:
        process.stop()
    moved = 0
    moved_names = []
    old_settings = paths.load_settings()
    try:
        if paths.path_exists(old):
            for p in sorted(old.iterdir()):
                dest = new / p.name
                if paths.path_exists(dest):
                    print("[!] " + str(dest) + " already exists — left "
                          + str(p) + " in place")
                    continue
                shutil.move(str(p), str(dest))
                moved += 1
                moved_names.append(p.name)
    except (OSError, ValueError, shutil.Error) as e:
        if was_running:
            process.start()
        _die("Could not move the data folder to {}: {} (some items may have "
             "moved; the data folder is still {} and nothing was recorded)"
             .format(new, getattr(e, "strerror", None) or e, old))

    def _rollback(reason):
        problems = []
        for name in reversed(moved_names):
            src = new / name
            dest = old / name
            try:
                if src.exists() and not dest.exists():
                    shutil.move(str(src), str(dest))
            except (OSError, ValueError, shutil.Error) as e:
                problems.append("could not move {} back: {}".format(
                    src, getattr(e, "strerror", None) or e))
        paths.set_data(old)
        try:
            paths.ensure_dirs()
        except (OSError, ValueError) as e:
            problems.append("could not restore old paths: {}".format(
                getattr(e, "strerror", None) or e))
        try:
            if old_settings:
                paths.save_settings(old_settings)
            elif paths.SETTINGS.exists():
                paths.SETTINGS.unlink()
        except OSError as e:
            problems.append("could not restore settings: {}".format(
                getattr(e, "strerror", None) or e))
        if was_running and not process.start():
            problems.append("the previously running stack did not restart")
        detail = ("; rollback notes: " + "; ".join(problems)) if problems else ""
        _die("Could not switch the data folder to {}: {}. Rolled back to {}{}"
             .format(new, reason, old, detail))

    paths.set_data(new)
    try:
        paths.ensure_dirs()
        settings = dict(old_settings)
        settings["data"] = str(new)
        paths.save_settings(settings)
        configgen.sync_data_paths()
        if was_running and not process.start():
            _rollback("the stack did not restart on the new data folder")
    except (OSError, ValueError, shutil.Error) as e:
        _rollback(getattr(e, "strerror", None) or e)
    return "Data folder: {} (moved {} item(s), stack {})".format(
        new, moved, "restarted" if was_running else "stopped")


def cmd_data(args):
    if not args.set:
        print("Data folder: " + str(paths.DATA))
        if paths.DATA.exists():
            entries = sorted(p.name for p in paths.DATA.iterdir()
                             if not p.name.startswith("."))
            print("  contents: " + (", ".join(entries) if entries else "(empty)"))
        return
    message = relocate_data(args.set)
    print(message)
    # A refusal is a refusal, not a result: `data --set` exiting 0 on "Refusing
    # to use ..." is what let a mistyped path look like a completed move.
    if message.startswith(("Refusing", "Invalid")):
        sys.exit(1)


def cmd_update(args):
    pins = versions.load_pins()
    if args.pin:
        for spec in args.pin:
            comp, _, tag = spec.partition("=")
            if comp not in versions.REPOS:
                _die("Unknown component: " + comp)
            pins[comp] = installer._validate_ref(comp, tag)
        versions.save_pins(pins)
        print("Pinned: " + ", ".join(args.pin))
        _reprovision(pins)
        return
    updates, errors = versions.check_updates()
    for e in errors:
        print("[!] " + e)
    if args.apply:
        for comp, cur, new in updates:
            if comp == "klipper" and pins[comp] == "master":
                print("  {:<10} master {} -> {}".format(comp, cur, new))
                continue  # branch-tracked; re-clone records the new SHA
            pins[comp] = new
            print("  {:<10} {} -> {}".format(comp, cur, new))
        if not updates:
            print("Already at newest upstream tags.")
        _reprovision(pins)
        return
    if not updates:
        print("All components at newest pinned upstream tags.")
        return
    print("Updates available (apply with: klipperformac update --apply):")
    for comp, cur, new in updates:
        print("  {:<10} {} -> {}".format(comp, cur, new))


def _reprovision(pins):
    if process.running() is not None:
        _die("Stack is running. Do: klipperformac down  (updates need a stopped stack)")
    print("Re-fetching from upstream...")
    for comp in ("klipper", "moonraker"):
        force = pins[comp] in ("master", "main")
        sha = installer.checkout(comp, pins[comp], force=force)
        pins[comp + "_sha"] = sha
        print("  {} @ {} ({})".format(comp, pins[comp], sha))
    installer.create_venv()
    installer.install_requirements()
    installer.fetch_mainsail(pins["mainsail"])
    installer.fetch_fluidd(pins["fluidd"])
    versions.save_pins(pins)
    installer.test_chelper_build()
    print("Updated. Restart with: klipperformac down && klipperformac up")


def cmd_open(args):
    webbrowser.open(paths.web_url())


def cmd_ui(args):
    pins = versions.load_pins()
    if args.name:
        if args.name not in ("mainsail", "fluidd"):
            _die("UI is 'mainsail' or 'fluidd'")
        if not (paths.WEB / args.name / "index.html").exists():
            print("[!] {} not downloaded yet — run: klipperformac setup".format(
                args.name))
        pins["ui"] = args.name
        versions.save_pins(pins)
        print("Default UI: " + args.name)
    url = paths.web_url()
    print(url)
    if not args.no_open:
        webbrowser.open(url)


def _copy_atomic(src, dst, what=None):
    """Copy onto a config path without ever leaving it half-written.

    shutil.copy2() opens the destination with O_TRUNC: a crash, a full disk or
    a SIGKILL in the middle of `presets --use` used to leave a truncated
    printer.cfg - the one file Klipper cannot start without, and the reason the
    version history exists in the first place. Copy to a sibling, then replace
    (which is atomic within one filesystem).

    With `what` set, a failed copy becomes one clean line on stderr and exit 1
    instead of a shutil traceback: a disk full, a read-only folder or a source
    that vanished mid-command is the user's problem to fix, and dumping the
    interpreter's internals made `presets --save` look like the tool had
    crashed. The destination is left exactly as it was either way."""
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        if what is not None:
            _die("Cannot {}: {} (nothing was written)".format(
                what, getattr(e, "strerror", None) or e))
        raise


def _history_dir():
    d = paths.CONFIG / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


HISTORY_KEEP = 10
# `presets --import` copies a file into the folder Moonraker serves over its
# config API; a config is a few kB, so anything bigger is the wrong file.
PRESET_IMPORT_MAX_BYTES = 2 * 1024 * 1024


def snapshot_printer_cfg(reason=""):
    """Copy the current printer.cfg into the version history.

    Deduped against the newest entry so repeated triggers (our own apply plus
    the GUI's edit watcher) store one version, not two. Keeps the newest
    HISTORY_KEEP files. Returns the new path, or None when nothing changed.
    """
    if not paths.PRINTER_CFG.exists():
        return None
    data = paths.PRINTER_CFG.read_bytes()
    hist = sorted(_history_dir().glob("printer-*.cfg"), key=lambda p: p.name)
    if hist and hist[-1].read_bytes() == data:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = ("-" + re.sub(r"[^A-Za-z0-9._-]+", "-", reason)[:40]) if reason else ""
    dest = _history_dir() / ("printer-{}{}.cfg".format(stamp, suffix))
    dest.write_bytes(data)
    hist.append(dest)
    while len(hist) > HISTORY_KEEP:
        try:
            hist.pop(0).unlink()
        except OSError:
            pass
    return dest


def catalog_names():
    """{name: path} of the pristine upstream example configs."""
    cdir = paths.KLIPPER / "config"
    if not cdir.exists():
        return {}
    out = {}
    for p in sorted(cdir.rglob("*.cfg")):
        out.setdefault(p.stem, p)
    return out


def auto_serial(cfg_path):
    """Give every [mcu*] section that has no real port a port. (lines, picked_ok)

    Two rules, in this order, and both only ever write a node that is attached
    and unclaimed (klippy holds a port exclusively, so handing one port to two
    sections is writing a printer that cannot start):

      1. identity: a section carrying the marker comment
         '# klipperformac:identity usb:VID:PID:SERIAL' gets its board resolved
         through detect's IOKit layer, which survives macOS renumbering the node
         on every replug. configgen.py does NOT write that marker (it is not
         ours to change), so this rule only fires on configs a user or a later
         wave bound on purpose.
      2. the pre-multi-MCU rule, unchanged in spirit: exactly ONE unclaimed
         candidate board and nothing resolved by identity, so write it.

    picked_ok is the honest answer to "does every section have a port now",
    read back from the file rather than trusted from a return value."""
    text = _cfg_text(cfg_path)
    rows = _parse_mcu_sections(text)
    if not rows:
        return _auto_serial_single(cfg_path, text)
    bare = [r for r in rows if not _real_serial(r.get("serial"))
            and not r.get("canbus")]
    need = [r for r in bare if not r.get("bad_name")]
    lines = [_mcu_bad_section_line(r) for r in bare if r.get("bad_name")]
    if not need:
        if lines:
            return lines, not _mcu_sections_missing(cfg_path)
        return ["Serial port already set in printer.cfg."], True
    ports = detect.scan()
    if not ports:
        return lines + ["No USB printer board detected. Plug it in, then pick "
                        "it under Serial device (or: klipperformac serial "
                        "--auto)."], False
    claimed = {r["serial"] for r in rows if r.get("serial")}
    # one ioreg, and only for a config that actually binds a section to an
    # identity: without a marker there is nothing to resolve.
    idents = _identity_map() if any(r.get("marker") for r in need) else {}
    used = set()
    pending = []
    for row in need:
        key = row.get("marker")
        if not key:
            pending.append(row)
            continue
        # A bound section is never handed whatever cable is left: the marker
        # says WHICH board, and pointing it at a different one is how a tool
        # head starts driving a heater it has no wires to.
        hit = _mcu_ident_by_key(idents, key)
        label = _mcu_section_label(row["mcu"])
        if hit is None:
            lines.append("[!] {} is bound to identity {}, which is not attached "
                         "right now: nothing was guessed.".format(label, key))
            continue
        if hit["path"] in claimed or hit["path"] in used:
            lines.append("[!] {}'s board {} is already claimed by another "
                         "section: nothing was written for it.".format(
                             label, hit["path"]))
            continue
        wrote, why = _write_mcu_serial(cfg_path, hit["path"], row["mcu"])
        if wrote:
            used.add(hit["path"])
            lines.append("Board connected: {}{} for {} (matched by identity "
                         "{})".format(hit["path"],
                                      _port_desc(ports, hit["path"]),
                                      label, key))
        else:
            lines.append("[!] could not write the serial: line for {}: {}"
                         .format(label, why or "no such section, or it refused"))
    free = [p for p in ports if p["path"] not in claimed
            and p["path"] not in used]
    if pending:
        if len(free) == 1 and len(pending) == 1:
            # The one guess this tool has always allowed: a single board and a
            # single section with no binding can only mean each other.
            row = pending[0]
            wrote, why = _write_mcu_serial(cfg_path, free[0]["path"],
                                           row["mcu"])
            if wrote:
                lines.append("Board connected: {}{} for {}".format(
                    free[0]["path"], _port_desc(ports, free[0]["path"]),
                    _mcu_section_label(row["mcu"])))
            else:
                lines.append("[!] could not write the serial: line for {}: {}"
                             .format(_mcu_section_label(row["mcu"]),
                                     why or "no such section, or it refused"))
        elif not free:
            lines.append("[!] no unclaimed USB board left for {}.".format(
                ", ".join(_mcu_section_label(r["mcu"]) for r in pending)))
        else:
            out = ["Multiple USB devices found - pick one (then: "
                   "klipperformac serial --set PATH MCUNAME):"]
            out += ["  " + p["path"] + (" (" + p["description"] + ")"
                                        if p["description"] else "")
                    for p in free]
            out.append("  {} still has no serial.".format(
                ", ".join(_mcu_section_label(r["mcu"]) for r in pending)))
            lines.extend(out)
    return lines, not _mcu_sections_missing(cfg_path)


def _mcu_section_label(name):
    """'[mcu tool]' for a message, and never None: a name this tool will not
    write still has to be named in the line that says so."""
    return detect.mcu_header(name) or "[mcu {}]".format(name)


def _mcu_bad_section_line(row):
    return ("[!] {} is not a section name this tool writes: klipper would read "
            "a different object than you typed. Fix it by hand; nothing was "
            "changed.".format(_mcu_section_label(row["mcu"])))


def _port_desc(ports, path):
    for p in ports:
        if p.get("path") == path:
            return (" (" + p["description"] + ")") if p.get("description") \
                else ""
    return ""


def _mcu_ident_by_key(idents, key):
    """The attached identity dict whose stable key equals a section's marker.

    Case-insensitive on purpose: ioreg publishes the USB serial upper case and
    a human pasting a key into printer.cfg usually does not care. None when the
    board is not attached right now (or the marker is not a key at all), which
    is a section that stays unassigned rather than a guessed one."""
    want = str(key or "").strip().lower()
    if not want:
        return None
    for ident in (idents or {}).values():
        if str(ident.get("key") or "").strip().lower() == want:
            return ident
        vid_pid, serial = _mcu_key_parts(want)
        if vid_pid and str(ident.get("vid_pid") or "").lower() == vid_pid \
                and not serial:
            return ident
    return None


def _mcu_sections_missing(cfg_path):
    """True when a section this tool can serve still has no real port.

    A section whose name cannot be written back is not "missing a port": it is
    reported by name in its own line, and a human has to fix the header."""
    rows = _parse_mcu_sections(_cfg_text(cfg_path))
    if not rows:
        return True
    return any(not r.get("bad_name") and not _real_serial(r.get("serial"))
               and not r.get("canbus") for r in rows)


def _auto_serial_single(cfg_path, text):
    """The original single-section path, kept for configs this parser does not
    read as MCU sections at all (no [mcu] header, a glued header, a vendor
    config that puts serial: somewhere else): behaviour must not regress."""
    lines = []
    m = re.search(r"(?m)^\s*serial\s*:(?!\s*$)(.*)$", text)
    if m and re.search(r"/dev/(cu|tty|usbserial|usbmodem)\S*", m.group(1)):
        return ["Serial port already set in printer.cfg."], True
    ports = detect.scan()
    if not ports:
        return ["No USB printer board detected. Plug it in, then pick it "
                "under Serial device (or: klipperformac serial --auto)."], False
    if len(ports) > 1:
        out = ["Multiple USB devices found - pick one (then: "
               "klipperformac serial --set PATH):"]
        out += ["  " + p["path"] + (" (" + p["description"] + ")"
                                    if p["description"] else "")
                for p in ports]
        return out, False
    if detect.write_serial(cfg_path, ports[0]["path"]):
        desc = (" (" + ports[0]["description"] + ")") \
            if ports[0]["description"] else ""
        return ["Board connected: " + ports[0]["path"] + desc], True
    return ["The config has no [mcu] section to put the serial port in."], False


def cmd_presets(args):
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    if args.library:
        names = catalog_names()
        if not names:
            _die("No upstream configs found yet. Run: klipperformac setup")
        print("\n".join(sorted(names)))
        return
    if args.use_library:
        names = catalog_names()
        src = names.get(args.use_library)
        if src is None:
            _die("No library config '{}'. Browse them with: "
                 "klipperformac presets --library".format(args.use_library))
        if paths.PRINTER_CFG.exists():
            snapshot_printer_cfg("before-library")
            _copy_atomic(paths.PRINTER_CFG, paths.CONFIG / "printer.cfg.bak",
                         "keep the current config as printer.cfg.bak")
        name = args.use_library
        _copy_atomic(src, paths.PRESETS / (name + ".cfg"),
                     "stage library config '{}' as a preset".format(name))
        _copy_atomic(src, paths.PRINTER_CFG,
                     "apply library config '{}'".format(name))
        print("Library config '{}' applied.".format(name))
        for line in auto_serial(paths.PRINTER_CFG)[0]:
            print(line)
        print("Restart to load it: klipperformac restart")
        return
    if args.history:
        hist = sorted(_history_dir().glob("printer-*.cfg"),
                      key=lambda p: p.name, reverse=True)
        if not hist:
            print("No saved versions yet (they are created automatically "
                  "when a config is applied or edited).")
            return
        for p in hist:
            print("  " + p.name)
        print("Restore with: klipperformac presets --restore NAME")
        return
    if args.restore:
        _safe_name(args.restore, "saved-version name")
        src = _history_dir() / args.restore
        if not src.exists():
            cand = sorted(_history_dir().glob("*" + args.restore + "*"))
            if len(cand) != 1:
                _die("No saved version matching '{}' (see: "
                     "klipperformac presets --history)".format(args.restore))
            src = cand[0]
        if paths.PRINTER_CFG.exists():
            snapshot_printer_cfg("before-restore")
        _copy_atomic(src, paths.PRINTER_CFG,
                     "restore saved version '{}'".format(src.name))
        print("Restored {} as the current config. "
              "Restart to load it.".format(src.name))
        return
    if args.snapshot:
        dest = snapshot_printer_cfg(args.snapshot if args.snapshot is True
                                    else args.snapshot)
        print("Version saved: " + (dest.name if dest
                                   else "unchanged since the last one"))
        return
    if args.save:
        if not paths.PRINTER_CFG.exists():
            _die("No printer.cfg to save")
        _safe_name(args.save, "preset name")
        dst = paths.PRESETS / (args.save + ".cfg")
        _copy_atomic(paths.PRINTER_CFG, dst,
                     "save the current config as preset '{}'".format(args.save))
        print("Saved preset '{}' -> {}".format(args.save, dst))
        return
    if args.use:
        _safe_name(args.use, "preset name")
        src = paths.PRESETS / (args.use + ".cfg")
        if not src.exists():
            avail = [p.stem for p in paths.PRESETS.glob("*.cfg")]
            _die("No preset '{}'. Available: {}".format(
                args.use, ", ".join(avail) or "none"))
        if paths.PRINTER_CFG.exists():
            snapshot_printer_cfg("before-preset")
            _copy_atomic(paths.PRINTER_CFG, paths.CONFIG / "printer.cfg.bak",
                         "keep the current config as printer.cfg.bak")
        _copy_atomic(src, paths.PRINTER_CFG,
                     "apply preset '{}'".format(args.use))
        print("Preset '{}' applied (previous config kept as printer.cfg.bak)."
              .format(args.use))
        if args.auto_serial:
            for line in auto_serial(paths.PRINTER_CFG)[0]:
                print(line)
        if process.running() is not None:
            print("Restart to load it: klipperformac restart")
        return
    if args.import_:
        src = os.path.expanduser(args.import_)
        if not os.path.isfile(src):
            _die("File not found: " + src)
        # A preset is copied into DATA/config/presets, which Moonraker serves
        # to any device that can reach it (/server/files/config/...). Mirroring
        # an upstream example config is the point of this flag; staging an
        # arbitrary file (a keychain, an ssh config, a 4 GB blob) into a
        # network-served folder is not, so the source has to look like a config.
        size = os.path.getsize(src)
        if size > PRESET_IMPORT_MAX_BYTES:
            _die("Refusing to import {}: it is {} bytes, over the {} byte "
                 "limit for presets. A printer.cfg is a few kB; import the "
                 "config, not the file it is not.".format(
                     src, size, PRESET_IMPORT_MAX_BYTES))
        with open(src, "rb") as f:
            head = f.read(min(size, 8192))
        if b"\0" in head:
            _die("Refusing to import {}: it is binary (contains a NUL byte), "
                 "not a Klipper config.".format(src))
        name = args.import_name or os.path.splitext(os.path.basename(src))[0]
        _safe_name(name, "preset name")
        dst = paths.PRESETS / (name + ".cfg")
        _copy_atomic(src, dst, "import '{}' as preset '{}'".format(src, name))
        print("Imported {} as preset '{}'".format(src, name))
        # A multi-MCU config imported blind is the classic "Printer is halted"
        # report: two [mcu*] sections on one node, or a section name klipper
        # reads as something else. Say it now, on stderr, and still import -
        # the config may be for a printer that is not attached to this Mac, and
        # blocking an import is not this command's call to make.
        try:
            for warn in _mcu_conflicts(dst):
                print("[!] " + warn, file=sys.stderr)
        except Exception as e:
            print("[!] multi-MCU check did not run: {}".format(e),
                  file=sys.stderr)
        return
    presets = sorted(paths.PRESETS.glob("*.cfg"))
    if not presets:
        print("No presets yet. Save one with: klipperformac presets --save NAME")
        print("(Upstream example configs are also presets: import one with")
        print(" klipperformac presets --import ~/.klipperformac/klipper/config/<model>.cfg)")
        return
    # Compared as bytes: a preset saved by an editor in cp1252 must still be
    # recognised, and read_text() would have crashed on that one byte (the
    # whole `presets` listing is a read-only view, so it never gets to choose).
    current = paths.PRINTER_CFG.read_bytes() if paths.PRINTER_CFG.is_file() \
        else None
    for p in presets:
        try:
            same = p.read_bytes() == current
        except OSError:
            same = False
        print("  {} {}".format("*" if same else " ", p.stem))
    print("(current marked *)")


def cmd_backup(args):
    """`klipperformac backup create|list|verify|restore|prune`.

    All of it lives in backup.py (the archive format, the zip-slip guard, the
    config validation). This is the thin dispatch the subparser points at, and
    backup is imported here rather than at module scope for the same reason tui
    is: cli.py must not need the whole module to parse `klipperformac status`."""
    from . import backup
    backup.dispatch(args)


def cmd_flash(args):
    """`klipperformac flash detect|uf2|dfu|serial|doctor|build`.

    flash.py owns every decision (what is attached, whether a .uf2/.bin is real,
    which transport a board is in, when the stack must come down first); this is
    the thin dispatch the subparser points at, and - like backup and tui - the
    module is imported here rather than at module scope so `klipperformac status`
    does not need it."""
    from . import flash
    code = flash.dispatch(args)
    if code:
        sys.exit(code)


def _klippy_console(cmd, timeout=6):
    """Send a Klipper console command (STATUS, RESTART, HELP, ...) over the
    host console PTY that klippy exposes as /tmp/printer. G-code via UIs
    goes through Moonraker instead."""
    import select
    import os as _os
    pty_path = os.environ.get("KLIPPERFORMAC_CONSOLE", "/tmp/printer")
    if not _os.path.exists(pty_path):
        _die("No Klipper console at {} (is the stack up?)".format(pty_path))
    try:
        fd = _os.open(pty_path, _os.O_RDWR | _os.O_NONBLOCK)
    except OSError as e:
        # A directory, an unreadable node or a unix socket is not a console:
        # same hint as "not there", never a traceback.
        _die("No Klipper console at {} ({}) (is the stack up?)".format(
            pty_path, getattr(e, "strerror", None) or e))
    lines = []
    try:
        _os.write(fd, (cmd + "\n").encode())
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.3)
            if not r:
                if lines:
                    break
                continue
            try:
                chunk = _os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                # EOF: the console peer is gone and select() would report the
                # fd ready forever (busy CPU until the deadline).
                break
            buf += chunk.decode(errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line:
                    lines.append(line)
                if line == "ok" or line.startswith("!!"):
                    deadline = 0
    finally:
        _os.close(fd)
    # First echo of our own command is usually present; drop it.
    if lines and lines[0].upper().startswith(cmd.upper()[:6]):
        lines = lines[1:]
    print("\n".join(lines) if lines else "(no response)")


def cmd_gcode(args):
    import json as _json
    from urllib.parse import quote
    import urllib.request
    base = "http://localhost:{}".format(paths.MOONRAKER_PORT)
    if args.status:
        url = base + "/printer/objects/query?print_stats&toolhead&heater_bed&extruder"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                out = r.read().decode()
        except Exception as e:
            _die("Moonraker not reachable (is it up?): " + str(e))
        try:
            answer = _json.loads(out)
        except ValueError:
            # Whitespace flattened: a response body must never forge a CLI line.
            _die("Moonraker's answer on port {} is not JSON (is another "
                 "service using that port?): {}".format(
                     paths.MOONRAKER_PORT,
                     " ".join(out[:120].split()) or "(empty response)"))
        print(_json.dumps(answer, indent=2)[:1500])
        return
    script = " ".join(args.script)
    if not script.strip():
        _die("Nothing to send. Usage: klipperformac gcode G1 X10 F600  "
             "(or: klipperformac gcode STATUS / --status)")
    if script.strip().upper() in ("STATUS", "RESTART", "FIRMWARE_RESTART", "HELP",
                                  "QUERY_ENDSTOPS"):
        _klippy_console(script)
        return
    url = base + "/printer/gcode/script?script=" + quote(script, safe="")
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except urllib.error.HTTPError as e:
        # Moonraker answered - it just refused. Its body says why ("Printer is
        # not ready", "Unknown command", a homing failure mid-script), and that
        # is the information the user needs; "HTTP Error 400: Unknown" blamed
        # reachability instead and sent people checking their network.
        try:
            body = e.read().decode(errors="replace")
        except Exception:
            body = ""
        reason = body
        try:
            answer = json.loads(body)
            reason = ((answer.get("error") or {}).get("message")
                      or json.dumps(answer)[:200])
        except ValueError:
            pass
        _die("Moonraker rejected the script (HTTP {}): {}".format(
            e.code, " ".join(str(reason).split())[:300] or "(no reason given)"))
    except Exception as e:
        _die("Moonraker not reachable (is it up?): " + str(e))
    print("sent: " + script + "  (output appears in: klipperformac logs)")


def cmd_cam(args):
    action = args.action
    if action == "on":
        ok, lines = cam.start()
    elif action == "off":
        ok, lines = cam.stop()
    elif action == "status":
        ok, lines = True, cam.status()
    elif action == "list":
        ok, lines = cam.list_devices()
    elif action == "snapshot":
        ok, lines = cam.snapshot(args.path)
    elif action == "record":
        # `cam record <name> [--seconds N] [--mic [NAME]]` (+ off | status):
        # the recording is written by the one camera process, since a Mac gives
        # each camera a single AVCaptureSession. cam.record() owns the whole
        # grammar, including the capability probe that refuses an old helper.
        ok, lines = cam.record(args.path, secs=args.seconds, mic=args.mic,
                               audio=None if args.audio is None
                               else args.audio == "on")
    elif action == "mics":
        ok, lines = cam.mics()
    elif action == "mic-permission":
        ok, lines = cam.mic_permission_status()
    elif action == "build":
        ok, lines = cam.build_helper()
    else:  # config
        kw = {}
        for flag in ("device", "width", "height", "fps", "quality", "mic"):
            value = getattr(args, flag, None)
            if value is None:
                continue
            if flag not in ("device", "mic"):
                lo, hi = {"width": (160, 4096), "height": (120, 4096),
                          "fps": (1, 60), "quality": (1, 100)}[flag]
                if not lo <= value <= hi:
                    _die("cam config: {} must be between {} and {} "
                         "(got {})".format(flag, lo, hi, value))
            kw[flag] = value
        if getattr(args, "audio", None) is not None:
            kw["audio"] = args.audio == "on"
        if kw:
            try:
                merged = cam.save_prefs(**kw)
            except OSError as e:
                _die("Could not write {} (your camera settings are "
                     "unchanged): {}".format(
                         paths.SETTINGS, getattr(e, "strerror", None) or e))
            ok = True
            lines = ["Saved camera settings: " + ", ".join(
                "{}={}".format(k, kw[k]) for k in sorted(kw)),
                "Turn the camera off and on again to apply."]
        else:
            ok = True
            p = cam.prefs()
            lines = ["Camera settings:"] + [
                "  {:<9} {}".format(k, p.get(k)) for k in
                ("device", "width", "height", "fps", "quality", "mic")]
            lines.append("  {:<9} {}".format(
                "audio", "on" if cam._as_bool(p.get("audio")) else "off"))
            lines.append("Change with: klipperformac cam config --fps 20 "
                         "--quality 90 --device <camera name>")
    print("\n".join(lines) if isinstance(lines, list) else str(lines))
    if not ok:
        sys.exit(1)


def cmd_batteryguard(args):
    """on|off|status|set|check for the low-battery watchdog.

    The guard is a REST client of upstream Moonraker - it pauses, cancels and
    brings the stack down through the same public API `klipperformac gcode`
    uses, and never writes a Klipper or Moonraker file (AGENTS.md 1)."""
    action = args.action
    on_off = {"on": True, "off": False}

    def _flag(value, name):
        if value is None:
            return None
        if value in on_off:
            return on_off[value]
        _die("batteryguard: --{} must be on or off (got '{}')".format(
            name, value))

    patch = {}
    for flag, key in (("pause_at", "pauseAt"), ("critical_at", "criticalAt"),
                      ("poll", "pollSec"), ("at_pause", "atPause"),
                      ("at_critical", "atCritical")):
        value = getattr(args, flag, None)
        if value is None:
            continue
        patch[key] = value
    for flag, key in (("resume_on_ac", "resumeOnAc"), ("notify", "notify")):
        value = _flag(getattr(args, flag, None), flag.replace("_", "-"))
        if value is not None:
            patch[key] = value
    if getattr(args, "dry_run", False) and action in ("on", "set"):
        patch["dryRun"] = True

    if action in ("on", "set"):
        # Thresholds are checked before anything is written or started: a typo
        # must not half-apply and must not launch a watchdog with a threshold
        # that can never fire (criticalAt above pauseAt).
        try:
            checked = batteryguard.validate(**patch)
        except ValueError as e:
            _die("batteryguard: " + str(e))
        if action == "on":
            checked["enabled"] = True
        try:
            batteryguard.save_battery_settings(**checked)
        except OSError as e:
            _die("Could not write {} (your settings are unchanged): {}".format(
                paths.SETTINGS, getattr(e, "strerror", None) or e))
        cfg = batteryguard.battery_settings()
        print("Battery settings: pause {}%, critical {}%, poll {}s, "
              "at pause {}, at critical {}, resume on AC {}, "
              "notifications {}, dry run {}.".format(
                  cfg["pauseAt"], cfg["criticalAt"], cfg["pollSec"],
                  cfg["atPause"], cfg["atCritical"],
                  "on" if cfg["resumeOnAc"] else "off",
                  "on" if cfg["notify"] else "off",
                  "on" if cfg["dryRun"] else "off"))
        if action == "set":
            print("Nothing was started or stopped. A running guard re-reads "
                  "these on its next poll; turn it on with: klipperformac "
                  "batteryguard on")
            return

    if action == "on":
        with _cli_lock():
            ok, lines = batteryguard.start()
        if ok and process.running() is None:
            lines.append("[!] The stack is down, so there is nothing to pause "
                         "yet; `klipperformac up` starts the guard with it.")
        print("\n".join(lines))
        if not ok:
            sys.exit(1)
        return

    if action == "off":
        try:
            batteryguard.save_battery_settings(enabled=False)
        except OSError as e:
            _die("Could not write {} (your settings are unchanged): {}".format(
                paths.SETTINGS, getattr(e, "strerror", None) or e))
        with _cli_lock():
            ok, lines = batteryguard.stop()
        print("\n".join(lines))
        print("Battery guard is off: nothing is watching the battery any more.")
        if not ok:
            sys.exit(1)
        return

    if action == "status":
        print("\n".join(batteryguard.status_lines()))
        return

    if action == "check":
        percent = getattr(args, "percent", None)
        source = getattr(args, "source", None)
        if percent is not None and batteryguard.clean_percent(percent) is None:
            _die("batteryguard check: --percent needs a battery percentage "
                 "0-100 (got {!r})".format(percent))
        if percent is not None and source is None:
            # An injected level without a source would be a guess about whether
            # this Mac is even at risk: refuse instead of defaulting to battery.
            _die("batteryguard check: --percent {} also needs --source "
                 "battery|ac".format(percent))
        dry = bool(getattr(args, "dry_run", False))
        ok, _lines, _episode = batteryguard.check(percent=percent,
                                                 source=source, dry=dry)
        if not ok:
            sys.exit(1)
        return

    _die("batteryguard: unknown action {!r}".format(action))


def cmd_powerloss(args):
    """The power-loss slice: the unclean-stop truth, the journal setting, and the
    way back to a journaled seam. Everything read-only unless the user names an
    action, and nothing here resumes a print by itself (upstream has no
    recover_at_seam to call - see klipperformac/powerloss.py)."""
    action = getattr(args, "action", None) or "status"
    if action == "on" or action == "off":
        wanted = {"journal": action == "on"}
        if getattr(args, "interval", None) is not None:
            wanted["intervalSec"] = args.interval
        cfg = _save_powerloss(**wanted)
        if action == "on":
            print("Power-loss journal ON: every {}s a print is active, one line "
                  "lands in\n  {}  (cap {} bytes, newest {} records kept)".format(
                      cfg["intervalSec"], powerloss.journal_path(),
                      cfg["maxBytes"], cfg["keepRecords"]))
            print("Nothing writes while no print is running, and nothing is ever "
                  "resumed automatically.")
            print("To keep polling while you print, run in another terminal (or "
                  "backgrounded):\n  klipperformac powerloss watch")
            print("`up` does not start that loop on its own: a resident disk "
                  "writer is yours to turn on.")
        else:
            print("Power-loss journal OFF (snapshots stop; the journal file and "
                  "the stop markers stay: {})".format(powerloss.folder()))
        return
    if action == "snapshot":
        record = powerloss.journal_tick(force=True)
        if record is None:
            print("Nothing to journal: no print is running (the journal records "
                  "only while klipper is printing or paused).")
            return
        print(powerloss.describe_record(record))
        return
    if action == "watch":
        cfg = powerloss.settings()
        if not cfg["journal"]:
            _die("The journal is off: klipperformac powerloss on")
        print("Journaling every {}s until the stack stops answering "
              "(Ctrl-C ends it).".format(cfg["intervalSec"]))
        written = powerloss.watch(interval=getattr(args, "interval", None),
                                  sweeps=getattr(args, "sweeps", None))
        print("Journaled {} snapshot(s).".format(written))
        return
    if action == "tail":
        lines = powerloss.tail_lines(getattr(args, "lines", 10))
        if not lines:
            print("Journal is empty (klipperformac powerloss on, then print "
                  "something).")
            return
        print("\n".join(lines))
        return

    # status: the whole picture, in the order a person acts on it.
    for line in powerloss.explain(stack_up=_powerloss_session_live()):
        print(line)


def cmd_powerloss_test(args):
    """LIVE bench test: cut a printing klipper and a printing moonraker dead with
    SIGKILL and prove what survives. Refuses to run without KLIPPERFORMAC_ALLOW_LIVE=1,
    because it ends a print on purpose (AGENTS.md: nothing destructive by
    default) and it drives a real board."""
    if os.environ.get("KLIPPERFORMAC_ALLOW_LIVE") != "1":
        _die("klipperformac powerloss-test SIGKILLs a printing klipper, then a "
             "printing Moonraker, on the board attached to this Mac.\n"
             "     That is what the test proves, so it only runs when you say "
             "the board is yours to cut off:\n"
             "         KLIPPERFORMAC_ALLOW_LIVE=1 klipperformac powerloss-test\n"
             "     (offline evidence for the same sequence: "
             "tests/powerloss/test_kill_sequence_machine.py)")
    if not _powerloss_stack_up():
        _die("The stack is not up (klipper and moonraker both have to be "
             "running): klipperformac up, then wait for `klippy ready`")
    # The one thing this command must never do is cut off a print the user cares
    # about. "the board is mine to kill" is not "this 9-hour print is mine to
    # ruin", so the live stack is asked what it is doing before anything starts.
    live = powerloss.query_stats() or {}
    if live.get("state") in ("printing", "paused"):
        _die("A print is live ({}: {} at byte {}). `powerloss-test` SIGKILLs "
             "klipper mid-print: finish, cancel or pause-and-decide on that job "
             "first.".format(live.get("state"), live.get("filename"),
                             live.get("file_position")))
    try:
        harness = powerloss.LiveHarness()
    except Exception as e:
        _die("powerloss-test: cannot start ({})".format(e))
    print("Power-loss kill sequence on the bench board. A G4-only test print is "
          "started (no motion, no heat, no extrusion) and killed twice. The "
          "journal is switched on for the run; nothing else in your settings is "
          "touched.")
    ok, _checks = powerloss.run_kill_sequence(harness,
                                              ticks=getattr(args, "ticks", 3))
    if not ok:
        print("[!] A check failed: the machine is left as the sequence found it. "
              "Read: klipperformac powerloss status")
        sys.exit(1)
    print("Power-loss kill sequence: PASS (journal survived both SIGKILLs, the "
          "marker told unclean from clean, the stack rebuilt).")


def cmd_login(args):
    """klipperformac login on|off|status - start the stack at macOS login.

    The mechanism is a per-user LaunchAgent running `klipperformac supervise`;
    the policy is the want file this command writes. `status` is read-only and
    takes no lock, because reading a plist and asking launchd a question must
    never be able to block behind a `setup`."""
    action = args.action
    if action == "status":
        status = launchd.login_status()
        if getattr(args, "json", False):
            print(json.dumps(status, indent=2, sort_keys=True))
            return
        for line in launchd.status_lines(status):
            print(line)
        return
    if getattr(args, "tick", None) is not None and action == "off":
        print("[!] --tick is ignored by `login off` (it only rewrites the "
              "agent it removes).")
    with _cli_lock():
        if action == "on":
            ok, lines = launchd.login_on(tick=args.tick,
                                         dry_run=args.dry_run)
        else:
            ok, lines = launchd.login_off()
    for line in lines:
        print(line)
    if not ok:
        sys.exit(1)


def cmd_supervise(args):
    """The LaunchAgent's own program. Typed by hand only to debug: it is a
    resident loop, and `klipperformac login on` is how it gets installed."""
    sys.exit(launchd.run(tick=getattr(args, "tick", None),
                         sweeps=getattr(args, "sweeps", None)))


def cmd_selftest(args):
    env = dict(os.environ)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = repo_root
    try:
        running = process.running() or {}
    except Exception:
        running = {}
    live_ports = _our_listener_ports() if not running else set()
    answered = {paths.MOONRAKER_PORT, paths.WEB_PORT, paths.FLUIDD_PORT} & live_ports
    if not any(running.get(name) for name in ("klipper", "moonraker",
                                              "web_mainsail", "web_fluidd")) \
            and not answered:
        print("[i]  stack appears stopped: selftest will report connection "
              "failures until `klipperformac up`.", flush=True)
    try:
        code = subprocess.call([str(paths.VENV_PY), "-m",
                                "klipperformac.tests.selftest"], env=env)
    except OSError as e:
        # No venv yet, or venv/bin/python without the exec bit or replaced by
        # a directory: one clean line naming the interpreter, never a traceback.
        _die("Could not run the selftest with {}: {} (run: klipperformac "
             "setup)".format(paths.VENV_PY, getattr(e, "strerror", None) or e))
    if code:
        # The child says what failed; its status is the answer AGENTS.md gates on.
        sys.exit(1 if code < 0 else code)
    # The child covers web + moonraker + klippy as one printer. Upstream takes N
    # MCUs, so the same per-MCU probe `doctor` prints runs here too - advisory
    # only: the exit code stays the child's answer, because a selftest that went
    # red over one unplugged toolboard would bury the three links that matter.
    try:
        rows = _mcu_runtime_rows()
        for tag, text, _verdict in _mcu_report_lines(rows=rows):
            print(tag + text)
        for tag, text, _verdict in _mcu_firmware_match_lines(rows=rows):
            print(tag + text)
        for tag, text, _verdict in _mcu_log_provenance_lines(rows=rows):
            print(tag + text)
    except Exception as e:
        print("[i]  mcu sections: not reported ({})".format(e))


def cmd_tui(args):
    from . import tui
    tui.main()


# ---- timing: what macOS is doing to klippy's scheduling (read-only) ------
#
# Everything in this block is a READ: `ps`, `pmset`, `ioreg`, our own log files
# and one localhost GET. Nothing is started, stopped, restarted or rewritten, so
# `klipperformac timing` is safe to run *during* a print - which is exactly when
# its answer matters.
#
# The thresholds come from the "Real-time scheduling" research plus pristine
# upstream Klipper (which stays untouched; these are reads of it, not edits):
#   - src/stepper.c:289 `queue_step oid=%c interval=%u count=%hu add=%hi` - one
#     command per step run, so the MCU owns every step edge and the host only
#     has to keep feeding it.
#   - klippy/chelper/serialqueue.c:110 MIN_REQTIME_DELTA 0.100 and :107 MIN_RTO
#     0.025 - commands are released ~100 ms ahead of need with a 25 ms
#     retransmit floor; the research's reading of that budget is that the host
#     must get scheduled again every ~250-450 ms to stay comfortably ahead.
#   - klippy/extras/error_mcu.py:34-46 - "Timer too close" and its siblings are
#     what host starvation looks like; klippy/mcu.py:906 turns a serial dropout
#     into "Lost communication with MCU".
#   - measured on this Mac by that research: srtt <= 3 ms, min half-RTT 64-84 us,
#     0 "Timer too close" across 6 klippy.log files, klippy at `ps` pri 31, and
#     no root-free way to ask for more: macOS has no SCHED_FIFO and a negative
#     nice is root-only (os.setpriority -> EACCES, os.nice(-1) -> EPERM), which
#     is why the only mitigation we ship is the QoS request in
#     compat.SITECUSTOMIZE_SRC plus this telemetry.

PRI_NORMAL = 31        # an unthrottled process reads 31 here (measured)
PRI_THROTTLED = 20     # UTILITY-class QoS lands in 20..30 (measured)
_THERM_TIMEOUT = 2.0   # `pmset -g therm` streams and never exits by itself
_LOG_BUDGET = 8 << 20  # at most 8 MiB of klippy.log: the command stays cheap
_LOADED_MCU_RE = re.compile(
    r"^Loaded MCU '([^']+)' \d+ commands \((.*)\)$")
_MACOS_MAX_PID = 99999  # macOS pids stop here; see process.py's _MAX_PID note

# The starvation error strings, as klippy prints them (extras/error_mcu.py:34-46
# is the list upstream itself treats as "the host computer is overloaded").
HOST_ERRORS = (
    ("timer_too_close", "Timer too close"),
    ("missed_scheduling", "Missed scheduling of next "),
    ("rescheduled_timer", "Rescheduled timer in the past"),
    ("stepper_too_far", "Stepper too far in past"),
    # mcu.py:906: a serial dropout. Often USB/board-side, not host starvation,
    # so it is reported but never fatal on its own.
    ("lost_communication", "Lost communication with MCU"),
)

# Fields the research pinned as the print's health indicators. A Stats line can
# carry several MCUs, so each key is read across the whole line: the worst MCU
# wins, and for rto the worst value is the lowest (its floor is MIN_RTO,
# serialqueue.c:107).
STAT_MAXIMA = ("srtt", "bytes_retransmit", "mcu_task_stddev", "sysload",
               "print_stall", "buffer_time", "print_time")
STAT_MINIMA = ("rto",)
STAT_FIELDS = STAT_MAXIMA + STAT_MINIMA

# Options that only a *Linux* Klipper knows. klippy rejects an option it has not
# seen: "Option 'rtpriority' is not valid in section 'printer'" comes out of
# klippy/configfile.py:440 and halts the printer, so adding it would be worse
# than useless - we detect it and say so, and never rewrite printer.cfg
# (AGENTS.md 2). Grow this list only from real user reports, each one verified
# against this Klipper checkout's docs/Config_Reference.md first.
LINUX_ONLY_CONFIG_OPTIONS = (("printer", "rtpriority"),)


def _valid_pid(value):
    """value as a pid worth asking `ps` about, or None (see process.py: a pid of
    0 is a process group and a huge one is not a process at all)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 < value <= _MACOS_MAX_PID else None


def _decode(raw):
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode(errors="replace")
    return str(raw)


def _timed_run(argv, timeout=5.0):
    """stdout of a read-only command, "" when it said nothing, None when it could
    not be run at all.

    The timeout is not cosmetic: `pmset -g therm` streams and never exits on its
    own (research finding), so calling it without one hangs the command - and
    when it does time out, the bytes it already printed are the answer, which is
    why TimeoutExpired.stdout is parsed instead of dropped."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return _decode(exc.stdout)
    except Exception:
        return None
    return _decode(done.stdout)


def _pri_class(pri):
    """Verdict for a `ps` priority, from the measurements in the research: 31 is
    the machine's normal posture, 20..30 is UTILITY-class (throttled), under 20
    is BACKGROUND/App-Nap territory a print does not survive."""
    if pri is None:
        return "unknown"
    if pri >= PRI_NORMAL:
        return "normal"
    if pri >= PRI_THROTTLED:
        return "throttled"
    return "background"


def _sched_posture(pid):
    """(nice, pri) of one pid from `ps -o pid=,ni=,pri=`; (None, None) if the
    read did not answer."""
    out = _timed_run(["ps", "-o", "pid=,ni=,pri=", "-p", str(pid)])
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == str(pid):
            try:
                return int(parts[1]), int(parts[2])
            except ValueError:
                return None, None
    return None, None


def _qos_env_state(pid):
    """'on'/'off'/'unknown': is KLIPPERFORMAC_QOS=ui in that pid's environment?

    This is the only observable evidence that klippy actually got our shim: a
    foreign sitecustomize.py in the same venv makes compat skip writing ours
    (compat/__init__.py writes only its own files), and then klippy boots with
    no QoS request and no complaint. The kernel hides another user's environment,
    so 'unknown' is a normal - and honest - answer."""
    out = _timed_run(["ps", "eww", "-p", str(pid), "-o", "command="])
    if out is None:
        return "unknown"
    if "KLIPPERFORMAC_QOS=ui" in out:
        return "on"
    if "KLIPPERFORMAC_QOS" in out:
        return "off"
    # `ps eww` appends KEY=VALUE pairs after argv; a line with none of them did
    # not show the environment at all.
    return "off" if re.search(r"\s[A-Z_][A-Z0-9_]*=", out) else "unknown"


def _pmset_block(text, header):
    """The `key value` pairs of one `pmset -g custom` block ("Battery Power:").
    That block is what a laptop falls back to the moment the cable comes out,
    and its lowpowermode/sleep are two of the things that kill a print."""
    block = {}
    inside = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            inside = stripped == header
            continue
        if inside:
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                block[parts[0]] = parts[1].strip()
    return block


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _power_external():
    """True on AC, False on battery, None when this Mac does not say.

    `ioreg -rn AppleSmartBattery`'s ExternalConnected, not `pmset -g batt`: the
    research found the pmset string varies ("Battery %", "AC attached", a
    charging line) while this key is a boolean in the plist."""
    out = _timed_run(["ioreg", "-rn", "AppleSmartBattery"])
    if not out:
        return None
    m = re.search(r'"ExternalConnected"\s*=\s*(Yes|No)', out)
    return None if m is None else m.group(1) == "Yes"


def _thermal(timeout=_THERM_TIMEOUT):
    """`pmset -g therm` as a dict (CPU_Speed_Limit is the one that matters: a
    capped CPU beats any QoS request). Always read under a timeout - see
    _timed_run()."""
    out = _timed_run(["pmset", "-g", "therm"], timeout=timeout)
    fields = {}
    for m in re.finditer(r"([A-Za-z_]{3,})\s*=\s*(\S+)", out or ""):
        fields[m.group(1)] = m.group(2)
    return fields


def _caffeinate_assert_held(pids):
    """True when a caffeinate pid of OURS holds PreventUserIdleSystemSleep.

    `pmset -g assertions` prints `pid 16010(caffeinate): ... named: "caffeinate
    command-line tool"`, so the check is the pid *and* the name in parentheses:
    the pid comes from state.json, which running() has already vouched for, and
    the name is what stops a recycled number - or somebody else's guard - from
    reading as our sleep prevention (process.py keeps its own caffeinate ledger
    for the same reason)."""
    if not pids:
        return None
    out = _timed_run(["pmset", "-g", "assertions"])
    if out is None:
        return None
    for line in out.splitlines():
        if "PreventUserIdleSystemSleep" not in line:
            continue
        m = re.search(r"\bpid\s+(\d+)\(caffeinate\)", line)
        if m and _int_or_none(m.group(1)) in pids:
            return True
    return False


def _klippy_log_files(limit=8):
    """klippy.log newest-first, then the rotated ones. The live log leads
    because the evidence a user cares about is the print happening now."""
    try:
        rotated = sorted((paths.KLIPPY_LOG.parent.glob(
            paths.KLIPPY_LOG.name + "*")), reverse=True)
    except OSError:
        rotated = []
    files = [paths.KLIPPY_LOG] + [f for f in rotated
                                  if f != paths.KLIPPY_LOG]
    return files[:limit]


def _read_log_tail(path, budget):
    """(text, truncated) for at most `budget` bytes of one log, taken from the
    END: a starvation event from the current print sits there, and an old
    startup banner does not change the verdict."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > budget:
                f.seek(size - budget)
                f.readline()  # a partial line is not evidence
                return _decode(f.read()), True
            return _decode(f.read()), False
    except OSError:
        return "", False


def _parse_loaded_mcu_line(line):
    """{'mcu','version','build_versions'} from one 'Loaded MCU ...' line."""
    m = _LOADED_MCU_RE.match((line or "").strip())
    if not m:
        return None
    payload = m.group(2)
    if " / " not in payload:
        return None
    version, build_versions = payload.split(" / ", 1)
    version = version.strip()
    build_versions = build_versions.strip()
    if not version or not build_versions:
        return None
    return {"mcu": m.group(1).strip(), "version": version,
            "build_versions": build_versions}


def _mcu_log_provenance(files=None, budget=_LOG_BUDGET):
    """Latest Loaded-MCU records per mcu from klippy.log, newest first."""
    left = budget
    out = {}
    for path in _klippy_log_files() if files is None else files:
        if left <= 0:
            break
        text, _cut = _read_log_tail(path, left)
        if text:
            left -= len(text.encode(errors="replace"))
        for raw in reversed(text.splitlines()):
            parsed = _parse_loaded_mcu_line(raw)
            if not parsed:
                continue
            name = parsed["mcu"]
            if name in out:
                continue
            parsed["log_file"] = str(path)
            out[name] = parsed
    return out


def _host_budget(files=None, budget=_LOG_BUDGET):
    """klippy.log's own account of host scheduling, regex-extracted.

    Values are the maxima over every log line the scan reached, plus the counts
    of the error strings upstream labels "host computer is overloaded"
    (extras/error_mcu.py:34-46). buffer_time is only counted while print_time
    advanced, because a parked print always reads 0.000."""
    counts = {key: 0 for key, _ in HOST_ERRORS}
    maxima = {}
    stats_lines = 0
    min_half_rtt = None
    scanned = []
    truncated = False
    left = budget
    for path in _klippy_log_files() if files is None else files:
        if left <= 0:
            truncated = True
            break
        text, cut = _read_log_tail(path, left)
        if text:
            scanned.append(str(path))
            left -= len(text.encode(errors="replace"))
        truncated = truncated or cut
        prev_print_time = None
        for line in text.splitlines():
            for key, needle in HOST_ERRORS:
                counts[key] += line.count(needle)
            m = re.search(r"min_half_rtt=([0-9.]+)", line)
            if m:
                min_half_rtt = float(m.group(1))
            if not line.startswith("Stats "):
                continue
            stats_lines += 1
            found = {}
            for key in STAT_FIELDS:
                values = [float(v) for v in re.findall(
                    r"\b%s=(-?[0-9.]+)" % key, line)]
                if not values:
                    continue
                # A Stats line can carry several MCUs: the worst one is the
                # answer, and for rto "worst" is the smallest floor.
                found[key] = (min(values) if key in STAT_MINIMA
                              else max(values))
            if "print_time" not in found:
                continue
            advancing = (prev_print_time is None
                         or found["print_time"] > prev_print_time)
            prev_print_time = found["print_time"]
            if not advancing:
                found.pop("buffer_time", None)
            for key, value in found.items():
                if key not in maxima:
                    maxima[key] = value
                elif key in STAT_MINIMA:
                    maxima[key] = min(maxima[key], value)
                else:
                    maxima[key] = max(maxima[key], value)
    return {
        "counts": counts,
        "srtt_max": maxima.get("srtt"),
        "rto_min": maxima.get("rto"),
        "bytes_retransmit_max": maxima.get("bytes_retransmit"),
        "mcu_task_stddev_max": maxima.get("mcu_task_stddev"),
        "sysload_max": maxima.get("sysload"),
        "print_stall_max": maxima.get("print_stall"),
        "buffer_time_max": maxima.get("buffer_time"),
        "print_time_max": maxima.get("print_time"),
        "min_half_rtt": min_half_rtt,
        "stats_lines": stats_lines,
        "log_files": scanned,
        "truncated": truncated,
    }


def _power_warning():
    """One honest line when a stack starts on battery. `caffeinate -s` asks for
    prevent-system-sleep, which - measured on this Mac and per the man page -
    only holds while on AC, so on battery just the idle-sleep assertion is
    keeping us alive and closing the lid still ends the print. Silent on AC and
    silent when this Mac does not say, because a guess is not a warning."""
    if _power_external() is not False:
        return
    print("[!] On battery: caffeinate's system-sleep assertion is AC-only, so "
          "lid-close still ends the print. Plug in before printing, and check "
          "Low Power Mode (`klipperformac timing`).")


def linux_only_options(text):
    """[(section, option)] for the Linux-only Klipper options in a printer.cfg.

    Pure function over text: it never reads or writes a config file, so the
    caller decides what (if anything) the user is told. `key: value` lines at
    column 0 start an option; an indented line continues the option above it, so
    only column-0 keys are tested."""
    hits = []
    section = ""
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if raw[:1] in (" ", "\t"):
            continue
        key = re.split(r"[:=]", stripped, maxsplit=1)[0].strip().lower()
        if (section, key) in LINUX_ONLY_CONFIG_OPTIONS:
            hits.append((section, key))
    return hits


def _printer_cfg_linux_only():
    """linux_only_options() on the live printer.cfg; [] when it cannot be read
    (an unreadable config is not this command's problem to report)."""
    try:
        text = paths.PRINTER_CFG.read_text(errors="replace")
    except (OSError, UnicodeError):
        return []
    try:
        return linux_only_options(text)
    except Exception:
        return []


def _tracked_pid(name):
    """The pid state.json recorded for a service, only when running() still
    vouches for it.

    `process.running()` answers {name: bool} (it re-validates pid + argv marker +
    boot time on every read) and the number itself lives in run/state.json, so
    a posture read needs both halves: the bool is the evidence the pid still is
    this service, the record is the number. A recycled pid never gets here."""
    state = process.running() or {}
    if not state.get(name):
        return None
    try:
        record = json.loads(paths.STATE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return _valid_pid(record.get(name))


def _timing_report():
    """The whole scheduling picture as one dict. Keys are snake_case and stable:
    `timing --json` is what the SwiftUI app reads, so a renamed key is a GUI
    bug. All cheap reads; no state change, no restart."""
    state = process.running() or {}
    live_ports = _our_listener_ports() if not state else set()
    moonraker_live = bool(state.get("moonraker")) or paths.MOONRAKER_PORT in live_ports
    pid = _tracked_pid("klipper")
    nice, pri = (None, None)
    if pid is not None:
        nice, pri = _sched_posture(pid)
    klipper = {
        "pid": pid,
        "running": bool(state.get("klipper")) or moonraker_live,
        "nice": nice,
        "pri": pri,
        "class": _pri_class(pri),
        "qos_env": _qos_env_state(pid) if pid is not None else "unknown",
    }
    caffeinate_pid = _tracked_pid("caffeinate")
    if caffeinate_pid is None and state.get("caffeinate") is False \
            and state.get("klipper"):
        # The stack is up and running() says our guard is NOT: nothing to look
        # for in `pmset -g assertions`, the sleep prevention is simply gone.
        caffeinate_held = False
    else:
        caffeinate_held = _caffeinate_assert_held(
            [caffeinate_pid] if caffeinate_pid else [])
    custom = _timed_run(["pmset", "-g", "custom"])
    battery = _pmset_block(custom, "Battery Power:")
    thermal = _thermal()
    power = {
        "external_connected": _power_external(),
        "battery_lowpowermode": _int_or_none(battery.get("lowpowermode")),
        "battery_sleep": _int_or_none(battery.get("sleep")),
        "caffeinate_pid": caffeinate_pid,
        "caffeinate_assert_held": caffeinate_held,
        "cpu_speed_limit": _int_or_none(thermal.get("CPU_Speed_Limit")),
        "cpu_scheduler_limit": _int_or_none(
            thermal.get("CPU_Scheduler_Limit")),
    }
    budget = _host_budget()
    printer_state = _klippy_state() if moonraker_live else None
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stack_running": bool(state) or bool(live_ports),
        "printer_state": printer_state,
        "klipper": klipper,
        "power": power,
        "host_budget": budget,
        "linux_only_options": ["[%s] %s" % (s, o)
                               for s, o in _printer_cfg_linux_only()],
        "problems": [],
    }
    report["problems"] = _timing_problems(report)
    report["ok"] = not report["problems"]
    return report


def _timing_problems(report):
    """The hard problems: what makes `timing` (and doctor) exit 1.

    The four the research says a user can act on - klippy throttled into the
    background, a print the host could not feed, no sleep guard of ours holding,
    and a print running on battery - plus a printer.cfg that names an option
    only a Linux Klipper has, because that one halts the printer on boot.
    Thermal caps and Low Power Mode are printed by _timing_lines() as warnings
    but never fatal here: the user cannot schedule around a throttled CPU, only
    work around it."""
    problems = []
    klipper = report["klipper"]
    if klipper["running"] and klipper["class"] == "background":
        problems.append(
            "klippy is at ps priority {} ({}) - macOS is App-Napping the "
            "process that talks to your MCU; start the stack from a Terminal "
            "or check the app's sleep settings".format(klipper["pri"],
                                                       klipper["class"]))
    counts = report["host_budget"]["counts"]
    if counts.get("timer_too_close"):
        problems.append('{} "Timer too close" in klippy.log: the host missed a '
                        "step deadline (upstream: this indicates the host "
                        "computer is overloaded)".format(
                            counts["timer_too_close"]))
    held = report["power"]["caffeinate_assert_held"]
    if klipper["running"] and held is False:
        problems.append("no klipperformac caffeinate is holding "
                        "PreventUserIdleSystemSleep (the guard died, or was "
                        "started by another instance)")
    on_battery = report["power"]["external_connected"] is False
    if on_battery and report["printer_state"] in ("printing", "paused"):
        problems.append(
            "printing on battery: caffeinate's system-sleep assertion is "
            "AC-only, so a lid close or an empty battery stops the print "
            "- plug in")
    if report["linux_only_options"]:
        problems.append("printer.cfg has Linux-only option(s) {}: klippy "
                        "rejects an unknown option and halts".format(
                            ", ".join(report["linux_only_options"])))
    return problems


def _timing_lines(report):
    """[(status, text)] for humans: the same facts as the JSON, each with the
    threshold it was judged against."""
    lines = []
    klipper = report["klipper"]
    power = report["power"]
    budget = report["host_budget"]
    if not klipper["running"]:
        lines.append(("[i]", "klippy is not running: timing says nothing about "
                             "a stopped stack (klipperformac up)"))
    else:
        posture = "klippy pid {} at ps pri {} ({})".format(
            klipper["pid"], klipper["pri"], klipper["class"])
        if klipper["class"] == "normal":
            posture += " - as good as macOS gets without root"
        elif klipper["class"] == "unknown":
            posture += " - ps did not answer"
        else:
            posture += " - fix this before printing"
        lines.append(("[ok]" if klipper["class"] == "normal" else
                      ("[i]" if klipper["class"] == "unknown" else "[!]"),
                      posture))
        qos = klipper["qos_env"]
        lines.append(("[ok]" if qos == "on" else "[i]",
                      "KLIPPERFORMAC_QOS in klippy's environment: {}{}".format(
                          qos,
                          "" if qos != "off" else
                          " (the sitecustomize QoS shim is not active - run "
                          "klipperformac restart)")))
    source = "on AC" if power["external_connected"] is True else (
        "on battery" if power["external_connected"] is False else
        "power source unknown")
    lpm = power["battery_lowpowermode"]
    lines.append(("[!]" if (power["external_connected"] is False and lpm)
                  else "[ok]",
                  "{}; battery Low Power Mode {} (sleep {}), CPU speed limit "
                  "{}%".format(source, "ON" if lpm else ("off" if lpm == 0
                                                         else "unknown"),
                               power["battery_sleep"],
                               power["cpu_speed_limit"])))
    if power["caffeinate_assert_held"] is True:
        lines.append(("[ok]", "caffeinate pid {} holds "
                             "PreventUserIdleSystemSleep".format(
                                 power["caffeinate_pid"])))
    elif power["caffeinate_assert_held"] is None:
        lines.append(("[i]", "caffeinate assertion: not checked (no tracked "
                             "caffeinate pid)"))
    else:
        lines.append(("[!]", "no caffeinate guard of ours is holding "
                             "PreventUserIdleSystemSleep"))
    if power["cpu_speed_limit"] is not None \
            and power["cpu_speed_limit"] < 100:
        lines.append(("[!]", "macOS is capping the CPU to {}% (thermal): QoS "
                             "cannot outrun a speed limit - cool the Mac "
                             "down".format(power["cpu_speed_limit"])))
    errs = " ".join("{}={}".format(key, budget["counts"][key])
                    for key, _ in HOST_ERRORS)
    starved = sum(budget["counts"][k] for k, _ in HOST_ERRORS
                  if k != "lost_communication")
    lines.append(("[!]" if starved else "[ok]",
                  "klippy.log over {} file(s){}: srtt max {}s, rto min {}s, "
                  "bytes_retransmit max {}, mcu_task_stddev max {}, sysload "
                  "max {}, print_stall max {}, buffer_time max {}s, last "
                  "min_half_rtt {}s | {}".format(
                      len(budget["log_files"]),
                      " (budget-truncated)" if budget["truncated"] else "",
                      budget["srtt_max"], budget["rto_min"],
                      budget["bytes_retransmit_max"],
                      budget["mcu_task_stddev_max"], budget["sysload_max"],
                      budget["print_stall_max"], budget["buffer_time_max"],
                      budget["min_half_rtt"], errs)))
    if budget["counts"]["lost_communication"] and not starved:
        lines.append(("[i]", "lost_communication is usually the USB link or the "
                             "board, not the host scheduler (klippy/mcu.py:906)"))
    lines.append(("[i]", "budget: the MCU owns every step edge "
                         "(src/stepper.c:289 queue_step); the host only has to "
                         "be scheduled every ~250-450 ms and pushes commands "
                         "100 ms ahead with a 25 ms retransmit floor "
                         "(klippy/chelper/serialqueue.c:110/:107)"))
    if budget["srtt_max"] is not None and budget["srtt_max"] > 0.003:
        lines.append(("[!]", "max srtt {}s is above the {}s this Mac measured "
                             "when healthy: the serial link is being "
                             "delayed".format(budget["srtt_max"], 0.003)))
    for option in report["linux_only_options"]:
        lines.append(("[!]", "printer.cfg carries '{}', which only a Linux "
                             "Klipper knows: klippy rejects it as an unknown "
                             "option (klippy/configfile.py:440) and halts. "
                             "Remove it from your config - macOS cannot raise "
                             "scheduler priority (no SCHED_FIFO, negative nice "
                             "is root-only), and step timing is done by the "
                             "MCU".format(option)))
    return lines


def cmd_timing(args):
    """`klipperformac timing` - read-only scheduling + power telemetry.

    Exit status: 0 healthy, 1 when something in _timing_problems() fired. The
    GUI consumes --json; the two share one report so they can never disagree."""
    report = _timing_report()
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True))
        sys.exit(0 if report["ok"] else 1)
    for tag, text in _timing_lines(report):
        print("{} {}".format(tag, text))
    for problem in report["problems"]:
        print("[!] " + problem)
    if not report["problems"]:
        print("[ok] nothing to fix: macOS is giving klippy the best scheduling "
              "it has without root")
    sys.exit(0 if report["ok"] else 1)


def _doctor_scheduling_lines(report):
    """The short form of `timing` for doctor: three lines plus whatever is hard.

    Same report object, so doctor and timing can never disagree about the same
    machine - and the gate AGENTS.md 4 puts on `doctor` now covers the one class
    of failure the rest of it cannot see: klippy alive, printer idle-looking,
    and the host scheduler about to eat a step deadline."""
    lines = []
    klipper = report["klipper"]
    power = report["power"]
    budget = report["host_budget"]
    if klipper["running"]:
        lines.append(("[ok]" if klipper["class"] == "normal" else
                      ("[i]" if klipper["class"] == "unknown" else "[!]"),
                      "host scheduling posture: klippy pri {} ({}) , QoS shim "
                      "{} in its environment".format(
                          klipper["pri"], klipper["class"], klipper["qos_env"])))
    else:
        lines.append(("[i]", "host scheduling posture: klippy not running "
                             "(klipperformac timing reads a live stack)"))
    starved = sum(budget["counts"][key] for key, _ in HOST_ERRORS
                  if key != "lost_communication")
    lines.append(("[ok]" if not starved else "[!]",
                  "klippy.log: {} starvation error(s), max srtt {}s, max "
                  "buffer_time {}s over {} file(s)".format(
                      starved, budget["srtt_max"], budget["buffer_time_max"],
                      len(budget["log_files"]))))
    on_battery = power["external_connected"] is False
    lpm = power["battery_lowpowermode"]
    if on_battery:
        # The old line promised "system sleep on battery cannot be fully
        # prevented" without saying why: `caffeinate -s` asserts prevent-system-
        # sleep, which the man page and this Mac both limit to AC power. So on
        # battery only the idle-sleep assertion holds and closing the lid still
        # ends the print - and Low Power Mode is a second, quieter killer.
        # Informational like it always was: `doctor` on a laptop on the couch is
        # not a failed install, but printing there is (see `timing`).
        lines.append(("[!]", "on battery: caffeinate's system-sleep assertion "
                             "is AC-only, so only the idle-sleep assertion "
                             "holds and lid-close still kills the print; Low "
                             "Power Mode is {} (it clocks the CPU down and "
                             "coalesces timers)".format(
                                 "ON" if lpm else ("off" if lpm == 0
                                                   else "unknown"))))
    else:
        lines.append(("[ok]" if power["external_connected"] is not None
                      else "[i]",
                      "power: {}, battery Low Power Mode {}, CPU speed limit "
                      "{}%".format(
                          "on AC" if power["external_connected"] else "unknown",
                          "off" if lpm == 0 else ("ON" if lpm else "unknown"),
                          power["cpu_speed_limit"])))
    lines.append(("[ok]" if power["caffeinate_assert_held"] is not False
                  else "[!]",
                  "sleep guard: caffeinate {} holding "
                  "PreventUserIdleSystemSleep".format(
                      "is" if power["caffeinate_assert_held"] else
                      ("was not checked"
                       if power["caffeinate_assert_held"] is None
                       else "is NOT"))))
    for option in report["linux_only_options"]:
        # Never rewritten, only named: printer.cfg is the user's (AGENTS.md 2).
        lines.append(("[!]", "printer.cfg carries '{}': a Linux-only option "
                             "this Klipper does not know, so klippy halts with "
                             "\"Option ... is not valid in section ...\" "
                             "(klippy/configfile.py:440). Remove it - macOS has "
                             "no SCHED_FIFO and negative nice is root-only; "
                             "step timing belongs to the MCU. See: "
                             "klipperformac timing".format(option)))
    lines += [("[!]", problem) for problem in report["problems"]]
    return lines


def cmd_doctor(args):
    ok = True
    for p in installer.check_prereqs(strict=False):
        print("[!] " + p)
        ok = False
    if paths.VENV_PY.exists():
        try:
            installer.test_chelper_build()
            print("[ok] c_helper builds with compat shims")
        except Exception as e:
            print("[!] " + str(e))
            ok = False
        try:
            installer.test_libsodium()
            print("[ok] libsodium present (Moonraker auth)")
        except Exception as e:
            print("[!] " + str(e))
            ok = False
    if paths.KLIPPER.exists():
        bad = installer.verify_pristine()
        if bad:
            for comp, out in bad:
                print("[!] {} checkout modified (not pristine):\n{}".format(comp, out))
            ok = False
        else:
            print("[ok] upstream checkouts pristine")
    else:
        print("[!] Not set up (run klipperformac setup)")
        ok = False
    # The power question is answered by _timing_report() below (ioreg's
    # ExternalConnected, not `pmset -g batt`, whose wording varies with whether
    # the battery is charging) - one read, one source of truth for doctor,
    # `timing` and the GUI.
    state = process.running()
    live_ports = _our_listener_ports() if state is None else set()
    moonraker_live = bool(state and state.get("moonraker")) \
        or paths.MOONRAKER_PORT in live_ports
    stack_live = _powerloss_session_live(state=state, live_ports=live_ports)
    if state:
        down = [n for n, st in state.items() if not st and n != "caffeinate"]
        if down:
            print("[!] services not running: " + ", ".join(down))
    elif moonraker_live:
        print("[i]  stack is not tracked, but Moonraker is answering on port {}"
              .format(paths.MOONRAKER_PORT))
    if moonraker_live:
        # A live python process is not a working printer. With a wrong [mcu]
        # serial klippy sits in `startup` forever and with an invalid section
        # it reports `error` ("Printer is halted") - both used to leave doctor
        # all green, which is the gate AGENTS.md 4 puts on "done".
        klippy = _klippy_state()
        if klippy == "ready":
            print("[ok] klipper state: ready")
        elif klippy is None:
            print("[i]  klipper state: unknown (moonraker did not answer "
                  "/printer/info)")
        else:
            print("[!] klipper state: {} - the printer cannot move "
                  "(klipperformac logs)".format(klippy))
            ok = False
        # ... and "ready" is still only the printer as a whole. Upstream takes N
        # MCUs (klippy/mcu.py:1246), so ask about each one: a [mcu tool] klippy
        # never loaded means cable 2 is not talking, and `status` alone cannot
        # see it. One read-only GET per section, 3 s timeout, and an MCU the
        # stack cannot describe is 'unknown', which never turns doctor red.
        try:
            mcu_rows = _mcu_runtime_rows()
            mcu_lines = _mcu_report_lines(rows=mcu_rows)
            fw_lines = _mcu_firmware_match_lines(rows=mcu_rows)
            log_lines = _mcu_log_provenance_lines(rows=mcu_rows)
        except Exception as e:
            # A doctor that stops printing halfway is worse than a missing line.
            print("[i]  mcu sections: not reported ({})".format(e))
            mcu_lines = []
            fw_lines = []
            log_lines = []
        for tag, text, verdict in mcu_lines:
            print(tag + text)
        for tag, text, verdict in fw_lines:
            print(tag + text)
        for tag, text, verdict in log_lines:
            print(tag + text)
        if _mcu_lines_fail(mcu_lines, klippy):
            ok = False
        if _mcu_firmware_lines_fail(fw_lines, klippy):
            ok = False
        if _mcu_log_lines_fail(log_lines, klippy):
            ok = False
    # Host scheduling posture: macOS gives klippy no real-time priority (no
    # SCHED_FIFO, negative nice is root-only), so the remaining risk is made
    # visible instead of promised away. Same report `klipperformac timing`
    # prints, so the two commands can never disagree about one machine.
    try:
        sched = _timing_report()
    except Exception as e:
        # Telemetry is never a reason doctor dies: say what could not be read.
        print("[i]  host scheduling posture: not measured ({})".format(e))
        sched = None
    if sched is not None:
        for tag, text in _doctor_scheduling_lines(sched):
            print("{} {}".format(tag, text))
        if not sched["ok"]:
            ok = False
    st_shaper = shaper.readiness()
    if st_shaper["needed"] and not st_shaper["healthy"]:
        print("[!] shaper: " + st_shaper["detail"])
        ok = False
    else:
        print("[ok] shaper: " + st_shaper["detail"])
    try:
        cam_configured = bool(paths.cam_settings()) or cam.running() is not None \
            or configgen.webcam_state()[0]
    except Exception:
        # An unreadable moonraker.conf is an unknown camera config, not a
        # reason for `doctor` to die before printing a single camera line.
        cam_configured = False
    for cam_ok, cam_msg in cam.doctor():
        if cam_ok:
            print("[ok] camera: " + cam_msg)
        elif cam_configured:
            print("[!] camera: " + cam_msg)
            ok = False
        else:
            print("[i]  camera: " + cam_msg)
    # The low-battery watchdog: honest lines about this Mac's battery, the
    # guard's own state, and whether printer.cfg can even be paused. Advisory
    # until the user turns the guard on - a desktop with no battery is not a
    # broken install, an enabled guard that cannot read the battery is.
    # The lid-close caveat is printed whatever the verdict, so the truth about
    # clamshell sleep is never hidden behind a green tick (AGENTS.md 5).
    try:
        guard_wanted = batteryguard.battery_settings()["enabled"]
        for b_ok, b_msg in batteryguard.doctor():
            if b_ok:
                print("[ok] " + b_msg)
            elif guard_wanted:
                print("[!] " + b_msg)
                ok = False
            else:
                print("[i]  " + b_msg)
    except Exception as e:
        print("[i]  battery: not reported ({})".format(e))
    # Power loss: did the last session end the way the user asked, and is the
    # byte offset of an active print being written down at all - plus the honest
    # answer to "what does upstream offer", which on macOS is: a job row flipped
    # to 'interrupted' (filename + elapsed time, never a position), no restart
    # awareness with [machine] provider none, no state file in klipper, and no
    # recover_at_seam anywhere upstream. ADVISORY, like the backup block below: a
    # badly-ended last session is history, not a broken install, and this
    # command's exit code is the install-correctness gate (AGENTS.md 4).
    try:
        for pl_ok, pl_msg in powerloss.doctor(
                query=bool(state and state.get("moonraker")),
                stack_up=stack_live):
            print(("[ok] " if pl_ok else "[!] ") + pl_msg)
    except Exception as e:
        print("[i]  power loss: not reported ({})".format(e))
    # Backups: the one question doctor can answer that nothing else does - "if
    # this Mac died tonight, is there anything to restore from?" Printed as
    # ADVISORY lines that never flip `ok`, deliberately: `doctor`'s exit code is
    # the install-correctness gate (AGENTS.md 4, and `make release-check` runs
    # it), and a healthy install whose owner has simply not run `backup create`
    # yet is not a broken install. Same reasoning as the on-battery line above,
    # and the same reason the camera lines only bite when a camera is configured.
    # The [!] tag is still the honest signal the user acts on. LAST, too, which
    # tests/backup/test_backup_doctor_wired.py pins: the restore question is the
    # one a person reads on the way out of the report.
    from . import backup as _backup          # local: cli.py must not need it
    try:
        for bak_ok, bak_msg in _backup.doctor_lines():
            print(("[ok] " if bak_ok else "[!] ") + bak_msg)
    except Exception as e:
        # A doctor that stops printing halfway is worse than a missing line.
        print("[!]  backups: not reported ({})".format(e))
    sys.exit(0 if ok else 1)


SYSTEM_DIRS = ("/", "/usr", "/bin", "/sbin", "/etc", "/var", "/tmp", "/private",
               "/opt", "/Volumes", "/Applications", "/System", "/Library",
               "/Users", "/home", "/root", "/dev", "/proc", "/Network")


def _deletable_tree(path, looks, what):
    """Why this folder may (or may not) be removed by `uninstall`.

    Every rmtree in this tool goes through here. A folder is only ours to delete
    when it is absolute, is not somebody else's whole tree (home root, a system
    folder, a filesystem root) and actually carries the shape of what it claims
    to be. `KLIPPERFORMAC_HOME=`, `=~` or a junk settings.json value used to be
    enough to make `uninstall` delete the current directory or the home folder -
    with ignore_errors=True on top, so it even reported success."""
    path = Path(path)
    if not path.exists():
        return None  # nothing to delete either way
    if not path.is_absolute():
        return ("{} '{}' is not an absolute path; refusing to delete it. "
                "Set KLIPPERFORMAC_HOME/KLIPPERFORMAC_DATA to a full path."
                .format(what, path))
    resolved = Path(os.path.realpath(str(path)))
    if resolved in (Path.home(), Path("/")) or str(resolved) in SYSTEM_DIRS:
        return ("[!] Refusing to delete {}: it is {} , not a Klipper-for-Mac "
                "folder.".format(path, "your home folder"
                                 if resolved == Path.home()
                                 else "a system folder"))
    if resolved != path and path.is_symlink():
        return ("[!] Refusing to delete {}: it is a symbolic link, not the "
                "folder itself.".format(path))
    if not looks(path):
        return ("[!] Refusing to delete {}: it does not look like {} "
                "(no venv/klipper/moonraker install layout). Remove it by hand "
                "if you really meant to.".format(
                    path, "an app folder" if "app" in what else "a data folder"))
    return None


def cmd_uninstall(args):
    """Remove the app (launcher symlink + app home). Asks before touching the
    user data folder unless --data/--keep-data says otherwise."""
    agent_problem = None
    with _cli_lock():
        # Step 0, before anything is deleted or killed: a loaded LaunchAgent
        # survives the tree it points at. uninstall removes the venv python and
        # the data folder, and launchd then respawns a supervisor whose
        # interpreter is gone - a crash loop with nowhere to write a log, on
        # every login, on a machine that no longer has Klipper for Mac.
        agent_ok, agent_lines = launchd.uninstall_teardown()
        for line in agent_lines:
            print(line)
        if not agent_ok:
            agent_problem = ("login agent at {} was left in place".format(
                launchd.plist_path()))
        killed = process.kill_all() + cam.kill_orphans() \
            + batteryguard.kill_orphans()
    if killed:
        print("Stopped {} process(es).".format(len(killed)))

    src_root = paths.APP_HOME / "src"
    links = [Path.home() / ".local" / "bin" / "klipperformac"]
    found = shutil.which("klipperformac")
    if found:
        links.append(Path(found))
    seen = set()
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        try:
            if link.is_symlink() and str(link.resolve()).startswith(str(src_root)):
                link.unlink()
                print("Removed launcher: " + str(link))
        except OSError:
            pass

    data = paths.DATA
    if args.keep_data:
        remove_data = False
    elif args.data:
        remove_data = True
    elif sys.stdin.isatty():
        remove_data = input(
            "\nDelete the data folder too? {} [y/N] ".format(data)
        ).strip().lower() in ("y", "yes")
    else:
        remove_data = False

    # The data folder lives inside the app folder by default now: rescue it
    # BEFORE the app-folder rmtree, or "keep my data" would destroy it.
    kept = None
    inside_app = str(data).startswith(str(paths.APP_HOME) + os.sep)
    if data.exists() and not remove_data and inside_app:
        base = paths.APP_HOME.parent / (paths.APP_HOME.name + ".data-kept")
        kept = base
        n = 1
        while kept.exists():
            n += 1
            kept = base.with_name(base.name + "-" + str(n))
        shutil.move(str(data), str(kept))
        print("Moved your data folder out of the app folder: " + str(kept))

    refused = []
    removed = []
    if agent_problem:
        # Uninstall that reports "Uninstalled." while a login item nobody can
        # unload is still in ~/Library/LaunchAgents would be a lie.
        print("[!] " + agent_problem)
        refused.append(agent_problem)
    for home in (paths.APP_HOME, Path("~/.maklipper").expanduser()):
        problem = _deletable_tree(home, paths.looks_like_app_home,
                                  "the app folder")
        if problem:
            print(problem)
            if home == paths.APP_HOME and home.exists():
                refused.append(problem)
            continue
        if not home.exists():
            continue
        try:
            shutil.rmtree(home)
        except OSError as e:
            # No ignore_errors=True: a half-removed install must not read as
            # "Uninstalled." while files (and a running venv) are still there.
            print("[!] Could not fully remove {}: {} (leftover files remain)"
                  .format(home, getattr(e, "strerror", None) or e))
            refused.append(str(home))
            continue
        print("Removed app folder: " + str(home))
        removed.append(home)
    if not removed and not refused:
        print("No app installation found at " + str(paths.APP_HOME))

    if remove_data:
        problem = None
        if not data.exists():
            if inside_app and not paths.APP_HOME.exists():
                print("Removed data folder (it sat inside the app folder): "
                      + str(data))
        elif paths.protected_data_dir(data):
            problem = ("[!] Refusing to delete: {} (your own documents/home "
                       "folder is not a data folder)".format(data))
        elif not paths.looks_like_data_dir(data):
            problem = ("[!] Refusing to delete the data folder {}: it does not "
                       "have a Klipper-for-Mac data layout (no "
                       "config/printer.cfg, no config/run/logs/gcodes set), so "
                       "it was never made here. Delete it yourself if you "
                       "meant to.".format(data))
        if problem:
            print(problem)
            refused.append(problem)
        elif data.exists():
            try:
                shutil.rmtree(data)
            except OSError as e:
                print("[!] Could not fully remove {}: {} (leftover files "
                      "remain)".format(data, getattr(e, "strerror", None) or e))
                refused.append(str(data))
            else:
                print("Removed data folder: " + str(data))
    elif kept is not None:
        print("Kept data folder: {} (point a fresh install at it with "
              "klipperformac data --set {})".format(kept, kept))
    else:
        print("Kept data folder: {} (delete it yourself if you want)".format(data))
    print("Uninstalled. Reload your shell or open a new terminal to clear the "
          "command from PATH cache: hash -r")
    if refused:
        _die("uninstall: {} folder(s) were refused, not deleted".format(
            len(refused)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from . import tui
        tui.main()
        return
    ap = argparse.ArgumentParser(
        prog="klipperformac",
        description="Klipper on macOS: pristine upstream, one command. "
                    "Run with no arguments for the interactive dashboard.",
    )
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    p = sub.add_parser("setup", help="fetch upstream components + verify build")
    p.add_argument("--with-shaper", action="store_true",
                   help="also build the Python 3.14 venv for SHAPER_CALIBRATE")
    p.add_argument("--install-python", action="store_true",
                   help="let setup install python@3.14 via Homebrew if missing")
    p = sub.add_parser("up", help="start klipper + moonraker + mainsail")
    p.add_argument("--open", action="store_true", help="open browser")
    sub.add_parser("down", help="stop everything")
    sub.add_parser("killall", help="force kill everything, incl. stray "
                                   "leftovers a plain down would miss")
    sub.add_parser("restart", help="stop then start")
    sub.add_parser("status", help="show state, pins, available updates")
    p = sub.add_parser("lan", help="open/close the stack to your local network")
    p.add_argument("state", nargs="?", choices=("on", "off"))
    p = sub.add_parser("batteryguard",
                       help="pause/cancel the print before the laptop battery "
                            "dies (localhost REST only, upstream untouched)")
    p.add_argument("action", choices=("on", "off", "status", "set", "check"))
    p.add_argument("--pause-at", type=int, metavar="PCT",
                   help="battery percent that pauses the print (default 15)")
    p.add_argument("--critical-at", type=int, metavar="PCT",
                   help="battery percent that cancels and stops the stack "
                        "(default 7, must not be above --pause-at)")
    p.add_argument("--poll", type=int, metavar="SEC",
                   help="seconds between battery reads (5-3600, default 30)")
    p.add_argument("--at-pause", choices=batteryguard.AT_PAUSE,
                   help="what to do at --pause-at (default pause)")
    p.add_argument("--at-critical", choices=batteryguard.AT_CRITICAL,
                   help="what to do at --critical-at (default cancel)")
    p.add_argument("--resume-on-ac", choices=("on", "off"),
                   help="resume the print when the cable comes back "
                        "(OPT-IN, default off): only a print this guard "
                        "paused is ever resumed")
    p.add_argument("--notify", choices=("on", "off"),
                   help="desktop notification + speech for each action")
    p.add_argument("--percent", metavar="PCT",
                   help="with check: judge this battery percent instead of "
                        "reading this Mac (needs --source)")
    p.add_argument("--source", choices=("battery", "ac"),
                   help="with check --percent: which power that percent is on")
    p.add_argument("--dry-run", action="store_true",
                   help="check: say what would happen and touch nothing "
                        "(no POST, no notification, no state write)")
    p = sub.add_parser("powerloss",
                       help="what a power loss costs on macOS: the unclean-stop "
                            "truth + a bounded journal of the print's byte offset "
                            "(localhost REST only, upstream untouched, never "
                            "auto-resumes)")
    p.add_argument("action", nargs="?", default="status",
                   choices=("status", "on", "off", "snapshot", "watch", "tail"))
    p.add_argument("--interval", type=float, metavar="SEC",
                   help="with on/watch: seconds between snapshots "
                        "({}-{}, default {})".format(
                            int(powerloss.MIN_INTERVAL),
                            int(powerloss.MAX_INTERVAL),
                            int(powerloss.DEFAULTS["intervalSec"])))
    p.add_argument("--sweeps", type=int, metavar="N",
                   help="with watch: stop after N polls instead of running "
                        "until the stack answers no more")
    p.add_argument("-n", "--lines", type=int, default=10,
                   help="with tail: how many snapshots to print (1-500)")
    p = sub.add_parser("powerloss-test",
                       help="LIVE: start a G4-only print, SIGKILL klipper then "
                            "moonraker, prove the journal and markers survive it "
                            "and the stack rebuilds. Needs "
                            "KLIPPERFORMAC_ALLOW_LIVE=1.")
    p.add_argument("--ticks", type=int, default=3,
                   help="journal polls while the first print is running")
    p = sub.add_parser("logs", help="view logs")
    p.add_argument("service", choices=("klipper", "moonraker"), default="klipper",
                   nargs="?")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p = sub.add_parser("serial", help="list USB ports and [mcu*] sections / "
                                      "set one section's serial")
    p.add_argument("--set", metavar="PATH")
    p.add_argument("--auto", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="machine-readable: {\"mcus\": [...], \"single\": bool} "
                        "for every [mcu*] section with its port, USB identity "
                        "and conflict; always exits 0 in read-only mode")
    p.add_argument("mcu", nargs="?", metavar="MCUNAME",
                   help="the [mcu ...] section to write with --set/--auto "
                        "(default: mcu, the timing master)")
    p = sub.add_parser("data", help="show or move the user data folder")
    p.add_argument("--set", metavar="PATH")
    p = sub.add_parser("update", help="check (or apply) upstream component updates")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--pin", action="append", metavar="COMP=TAG")
    sub.add_parser("open", help="open Mainsail in browser")
    p = sub.add_parser("ui", help="open or switch web UI (mainsail|fluidd)")
    p.add_argument("name", nargs="?", choices=("mainsail", "fluidd"))
    p.add_argument("--no-open", action="store_true")
    p = sub.add_parser("presets",
                       help="list / save / use / import printer.cfg presets")
    p.add_argument("--save", metavar="NAME")
    p.add_argument("--use", metavar="NAME")
    p.add_argument("--auto-serial", action="store_true",
                   help="with --use/--use-library: fill [mcu] serial from "
                        "the detected USB board")
    p.add_argument("--library", action="store_true",
                   help="list upstream example configs (klipper/config)")
    p.add_argument("--use-library", metavar="NAME",
                   help="apply an upstream example config as a preset")
    p.add_argument("--history", action="store_true",
                   help="list saved config versions (last 10)")
    p.add_argument("--restore", metavar="NAME",
                   help="restore a saved version as the current config")
    p.add_argument("--snapshot", nargs="?", const="manual", metavar="REASON",
                   help="save the current config as a version")
    p.add_argument("--import", dest="import_", metavar="PATH")
    p.add_argument("--import-name", metavar="NAME")
    p = sub.add_parser("backup",
                       help="archive / verify / restore the whole data folder")
    p.add_argument("action", choices=("create", "list", "verify", "restore",
                                      "prune"))
    p.add_argument("--from", dest="from_", metavar="NAME",
                   help="with verify/restore: a name from "
                        "'klipperformac backup list', or 'latest'")
    p.add_argument("--with-logs", "--include-logs", dest="with_logs",
                   action="store_true",
                   help="with create: also archive data/logs (big; off by "
                        "default)")
    p.add_argument("--allow-running", action="store_true",
                   help="with create: snapshot a running stack anyway (its "
                        "sqlite database may be copied half-written)")
    p.add_argument("--keep", type=int, metavar="N",
                   help="with prune: keep the newest N backups (default 10)")
    p.add_argument("--dry-run", action="store_true",
                   help="with prune: show what would be deleted")
    p = sub.add_parser("gcode", help="send gcode to Klipper or query status")
    p.add_argument("script", nargs="*")
    p.add_argument("--status", action="store_true")
    p = sub.add_parser("cam", help="Mac webcam for the printer dashboard "
                                   "(localhost-only MJPEG stream)")
    p.add_argument("action", choices=("on", "off", "status", "list",
                                      "snapshot", "build", "config",
                                      "record", "mics", "mic-permission"))
    p.add_argument("path", nargs="?",
                   help="snapshot output file (default: data folder timelapse); "
                        "with record: the video name or path under "
                        "data/recordings, or off | status")
    p.add_argument("--device", metavar="NAME", help="camera name or index")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--fps", type=int)
    p.add_argument("--quality", type=int, help="JPEG quality 1-100")
    p.add_argument("--seconds", metavar="N",
                   help="with record: close the file and stop by itself after "
                        "N seconds (the helper does this, not a timer here)")
    p.add_argument("--mic", metavar="NAME", nargs="?", const="", default=None,
                   help="with config: the microphone to record with; with "
                        "record: add sound now - bare for the one macOS picks, "
                        "or a name from 'klipperformac cam mics'")
    p.add_argument("--audio", choices=("on", "off"),
                   help="with config: let recordings carry sound; with "
                        "record: insist on (or take back) sound for this one")
    p.add_argument("--show", action="store_true",
                   help="with config: show current settings")
    sub.add_parser("selftest", help="verify web + moonraker + klippy links")
    sub.add_parser("doctor", help="environment + build + power checks")
    p = sub.add_parser("login",
                       help="start the stack at macOS login (LaunchAgent + "
                            "supervisor); `down` always wins over it")
    p.add_argument("action", choices=("on", "off", "status"))
    p.add_argument("--tick", type=float, metavar="SEC",
                   help="seconds between supervisor sweeps (agent plist; "
                        "default {}s, floor {})".format(int(launchd.DEFAULT_TICK),
                                                        int(launchd.MIN_TICK)))
    p.add_argument("--dry-run", action="store_true",
                   help="on: show the agent that would be written and loaded, "
                        "touch and launch-load nothing")
    p.add_argument("--json", action="store_true",
                   help="status: one machine-readable object (the GUI reads "
                        "this)")
    p = sub.add_parser("supervise",
                       help="internal: the resident loop the login agent runs "
                            "(see: klipperformac login on)")
    p.add_argument("--tick", type=float, metavar="SEC",
                   help="seconds between sweeps (default {})".format(
                       int(launchd.DEFAULT_TICK)))
    p.add_argument("--sweeps", type=int, metavar="N", help=argparse.SUPPRESS)
    p = sub.add_parser("timing",
                       help="read-only scheduling + power telemetry: what "
                            "macOS is doing to klippy right now")
    p.add_argument("--json", action="store_true",
                   help="one machine-readable object (the GUI reads this)")
    sub.add_parser("tui", help="interactive dashboard (default with no args)")
    p = sub.add_parser("uninstall", help="remove the app; asks before deleting "
                                         "the data folder")
    p.add_argument("--data", action="store_true",
                   help="also delete the data folder without asking")
    p.add_argument("--keep-data", action="store_true",
                   help="never delete the data folder")
    # `flash` keeps its whole flag surface in flash.py (add_parser) so this file
    # gains one call here and one handler entry below, and nothing else.
    from . import flash as _flash
    _flash.add_parser(sub)

    args = ap.parse_args(argv)
    handlers = {
        "setup": cmd_setup, "up": cmd_up, "down": cmd_down,
        "killall": cmd_killall, "restart": cmd_restart,
        "status": cmd_status, "logs": cmd_logs, "serial": cmd_serial,
        "lan": cmd_lan,
        "batteryguard": cmd_batteryguard,
        "powerloss": cmd_powerloss,
        "powerloss-test": cmd_powerloss_test,
        "data": cmd_data,
        "update": cmd_update, "open": cmd_open, "doctor": cmd_doctor,
        "timing": cmd_timing,
        "ui": cmd_ui, "presets": cmd_presets, "gcode": cmd_gcode,
        "backup": cmd_backup,
        "cam": cmd_cam,
        "flash": cmd_flash,
        "selftest": cmd_selftest, "tui": cmd_tui, "uninstall": cmd_uninstall,
        "login": cmd_login, "supervise": cmd_supervise,
    }
    handlers[args.cmd](args)
