"""klipperformac command-line interface: setup | up | down | killall | status |
logs | serial | update | open | doctor | timing | backup | cam | uninstall."""
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

from . import cam, configgen, detect, installer, paths, process, shaper, versions


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
        else:
            print("Already running.")
            return
    else:
        note = shaper.interpreter_for_klipper()[1]
        if not process.start():
            sys.exit(1)  # start() said why (port held, record not writable)
        if note:
            print(note)
        print("Klipper stack starting:")
        print("  Mainsail:  " + paths.ui_url("mainsail"))
        print("  Fluidd:    " + paths.ui_url("fluidd"))
        print("  Moonraker: http://localhost:{}".format(paths.MOONRAKER_PORT))
        for line in _lan_urls():
            print(line)
        print("  Logs: klipperformac logs   Stop: klipperformac down")
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


def cmd_down(args):
    with _cli_lock():
        stopped = process.stop()
    cam_off, cam_lines = cam.stop()
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
    moonraker/caffeinate)."""
    with _cli_lock():
        killed = process.kill_all() + cam.kill_orphans()
    if not killed:
        print("Nothing to kill.")
        return
    print("Killed {} process(es):".format(len(killed)))
    for name, pid in killed:
        print("  {:<12} pid {}".format(name, pid))


def cmd_restart(args):
    with _cli_lock():
        process.stop()
        _cmd_up(args)


def cmd_status(args):
    state = process.running()
    if state is None:
        print("stopped")
        left = process.our_listeners()
        if left:
            print("  [!] {} still answering although no stack is tracked: "
                  "klipperformac killall".format(
                      ", ".join("port {}".format(p) for p, _ in left)))
        for line in _lan_urls(indent="  "):
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


def cmd_serial(args):
    ports = detect.scan()
    idents = _identity_map() if ports else {}
    rows, claims = _configured_mcus()
    for p in ports:
        desc = (" (" + p["description"] + ")") if p["description"] else ""
        tail = _identity_tail(idents.get(p["path"]), claims.get(p["path"]))
        print(p["path"] + desc + ("  " + tail if tail else ""))
    target = args.set
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
        notes = _serial_notes(ports, rows, claims)
        if not ports:
            print("No USB serial devices found. Connect the printer board and "
                  "retry.")
            for note in notes:
                print(note)
            return
        for note in notes:
            print(note)
        print("\nTo use one: klipperformac serial --set /dev/cu.usbserial-XXXX "
              "(or --auto for the first above)")
        return
    # From here the user asked for a specific port: an exit 0 that wrote
    # nothing (no device scanned, no printer.cfg, no [mcu]) is a lie that CI,
    # scripts and the GUI all believe. Every failure path below is a real error.
    if not paths.PRINTER_CFG.exists():
        _die("No printer.cfg yet. Run: klipperformac setup")
    if re.search(r"[\r\n\0]", target):
        _die("Invalid device path: must not contain newlines")
    if not os.path.exists(target):
        _die("{} is not a device that exists right now: nothing was written "
             "to {}".format(target, paths.PRINTER_CFG))
    if detect.write_serial(paths.PRINTER_CFG, target):
        print("Wrote serial to " + str(paths.PRINTER_CFG))
        if target not in [p["path"] for p in ports]:
            print("[!] {} was not on the scanned list; a device node that "
                  "exists is accepted anyway (the scanner only knows the "
                  "common USB-serial name families)".format(target))
    else:
        _die("Could not find [mcu] serial: line in " + str(paths.PRINTER_CFG))


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
    if new.exists():
        new = new.resolve()
    old = paths.DATA
    resolved_old = old.resolve() if old.exists() else old
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
    if new.is_dir():
        entries = [p for p in new.iterdir() if not p.name.startswith(".")]
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
    try:
        if old.exists():
            for p in sorted(old.iterdir()):
                dest = new / p.name
                if dest.exists():
                    print("[!] " + str(dest) + " already exists — left "
                          + str(p) + " in place")
                    continue
                shutil.move(str(p), str(dest))
                moved += 1
    except (OSError, ValueError, shutil.Error) as e:
        if was_running:
            process.start()
        _die("Could not move the data folder to {}: {} (some items may have "
             "moved; the data folder is still {} and nothing was recorded)"
             .format(new, getattr(e, "strerror", None) or e, old))
    paths.set_data(new)
    paths.ensure_dirs()
    settings = paths.load_settings()
    settings["data"] = str(new)
    _save_settings(settings)
    configgen.sync_data_paths()
    if was_running:
        process.start()
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
            pins[comp] = tag
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
    """Fill [mcu] serial: with the detected USB board when the config does
    not already carry a real port. Returns (lines, picked_ok)."""
    lines = []
    try:
        text = cfg_path.read_text(errors="replace")
    except OSError:
        text = ""
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
        desc = (" (" + ports[0]["description"] + ")") if ports[0]["description"] else ""
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
    elif action == "build":
        ok, lines = cam.build_helper()
    else:  # config
        kw = {}
        for flag in ("device", "width", "height", "fps", "quality"):
            value = getattr(args, flag)
            if value is None:
                continue
            if flag != "device":
                lo, hi = {"width": (160, 4096), "height": (120, 4096),
                          "fps": (1, 60), "quality": (1, 100)}[flag]
                if not lo <= value <= hi:
                    _die("cam config: {} must be between {} and {} "
                         "(got {})".format(flag, lo, hi, value))
            kw[flag] = value
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
                ("device", "width", "height", "fps", "quality")]
            lines.append("Change with: klipperformac cam config --fps 20 "
                         "--quality 90 --device <camera name>")
    print("\n".join(lines) if isinstance(lines, list) else str(lines))
    if not ok:
        sys.exit(1)


def cmd_selftest(args):
    env = dict(os.environ)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = repo_root
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
    pid = _tracked_pid("klipper")
    nice, pri = (None, None)
    if pid is not None:
        nice, pri = _sched_posture(pid)
    klipper = {
        "pid": pid,
        "running": bool(state.get("klipper")),
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
    printer_state = _klippy_state() if state.get("moonraker") else None
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stack_running": bool(state),
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
    if state:
        down = [n for n, st in state.items() if not st and n != "caffeinate"]
        if down:
            print("[!] services not running: " + ", ".join(down))
    if state and state.get("moonraker"):
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
    # Backups: the one question doctor can answer that nothing else does - "if
    # this Mac died tonight, is there anything to restore from?" Printed as
    # ADVISORY lines that never flip `ok`, deliberately: `doctor`'s exit code is
    # the install-correctness gate (AGENTS.md 4, and `make release-check` runs
    # it), and a healthy install whose owner has simply not run `backup create`
    # yet is not a broken install. Same reasoning as the on-battery line above,
    # and the same reason the camera lines only bite when a camera is configured.
    # The [!] tag is still the honest signal the user acts on.
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
    with _cli_lock():
        killed = process.kill_all() + cam.kill_orphans()
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
    p = sub.add_parser("logs", help="view logs")
    p.add_argument("service", choices=("klipper", "moonraker"), default="klipper",
                   nargs="?")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p = sub.add_parser("serial", help="list USB printer ports / set [mcu] serial")
    p.add_argument("--set", metavar="PATH")
    p.add_argument("--auto", action="store_true")
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
                                      "snapshot", "build", "config"))
    p.add_argument("path", nargs="?",
                   help="snapshot output file (default: data folder timelapse)")
    p.add_argument("--device", metavar="NAME", help="camera name or index")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--fps", type=int)
    p.add_argument("--quality", type=int, help="JPEG quality 1-100")
    p.add_argument("--show", action="store_true",
                   help="with config: show current settings")
    sub.add_parser("selftest", help="verify web + moonraker + klippy links")
    sub.add_parser("doctor", help="environment + build + power checks")
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
        "data": cmd_data,
        "update": cmd_update, "open": cmd_open, "doctor": cmd_doctor,
        "timing": cmd_timing,
        "ui": cmd_ui, "presets": cmd_presets, "gcode": cmd_gcode,
        "backup": cmd_backup,
        "cam": cmd_cam,
        "flash": cmd_flash,
        "selftest": cmd_selftest, "tui": cmd_tui, "uninstall": cmd_uninstall,
    }
    handlers[args.cmd](args)
