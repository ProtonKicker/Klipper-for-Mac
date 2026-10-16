"""klipperformac command-line interface: setup | up | down | killall | status |
logs | serial | update | open | doctor | cam | uninstall."""
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


def cmd_serial(args):
    ports = detect.scan()
    for p in ports:
        desc = (" (" + p["description"] + ")") if p["description"] else ""
        print(p["path"] + desc)
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
        if not ports:
            print("No USB serial devices found. Connect the printer board and "
                  "retry.")
            return
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
    try:
        batt = subprocess.run(["pmset", "-g", "batt"],
                              capture_output=True).stdout.decode()
    except Exception:
        batt = ""
    if "Battery" in batt and "AC" not in batt:
        print("[!] Running on battery: system sleep on battery cannot be fully "
              "prevented; prints may die if the lid closes or battery empties.")
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
    sub.add_parser("tui", help="interactive dashboard (default with no args)")
    p = sub.add_parser("uninstall", help="remove the app; asks before deleting "
                                         "the data folder")
    p.add_argument("--data", action="store_true",
                   help="also delete the data folder without asking")
    p.add_argument("--keep-data", action="store_true",
                   help="never delete the data folder")

    args = ap.parse_args(argv)
    handlers = {
        "setup": cmd_setup, "up": cmd_up, "down": cmd_down,
        "killall": cmd_killall, "restart": cmd_restart,
        "status": cmd_status, "logs": cmd_logs, "serial": cmd_serial,
        "lan": cmd_lan,
        "data": cmd_data,
        "update": cmd_update, "open": cmd_open, "doctor": cmd_doctor,
        "ui": cmd_ui, "presets": cmd_presets, "gcode": cmd_gcode,
        "cam": cmd_cam,
        "selftest": cmd_selftest, "tui": cmd_tui, "uninstall": cmd_uninstall,
    }
    handlers[args.cmd](args)
