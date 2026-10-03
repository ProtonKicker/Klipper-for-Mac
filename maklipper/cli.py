"""maklipper command-line interface: setup | up | down | status | logs |
serial | update | open | doctor."""
import argparse
import os
import subprocess
import sys
import time
import webbrowser

from . import detect, installer, paths, process, versions


def _die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def cmd_setup(args):
    installer.setup()


def cmd_up(args):
    paths.ensure_dirs()
    if not paths.VENV_PY.exists():
        _die("Not set up yet. Run: maklipper setup")
    if process.running() is not None:
        print("Already running.")
    else:
        process.start()
        print("Klipper stack starting:")
        print("  Mainsail:  " + paths.ui_url("mainsail"))
        print("  Fluidd:    " + paths.ui_url("fluidd"))
        print("  Moonraker: http://localhost:{}".format(paths.MOONRAKER_PORT))
        print("  Logs: maklipper logs   Stop: maklipper down")
        _wait_ready()
        if getattr(args, "open", False):
            webbrowser.open(paths.web_url())


def _wait_ready(timeout=20):
    import json as _json
    import time
    import urllib.request
    url = "http://localhost:{}/server/info".format(paths.MOONRAKER_PORT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                info = _json.loads(r.read().decode()).get("result", {})
            state = info.get("klippy_state", "?")
            conn = "connected" if info.get("klippy_connected") else "not yet"
            print("  ready: moonraker up, klippy {} (state: {})".format(conn, state))
            return
        except Exception:
            time.sleep(0.7)
    print("[!] Moonraker did not answer within {}s — check: maklipper logs"
          .format(timeout))


def cmd_down(args):
    if process.stop():
        print("Stopped.")
    else:
        print("Was not running.")


def cmd_restart(args):
    process.stop()
    cmd_up(args)


def cmd_status(args):
    state = process.running()
    if state is None:
        print("stopped")
        return
    pins = versions.load_pins()
    up = {n: st for n, st in state.items() if st}
    print("running" if up else "stopping...")
    for name in process.SERVICES:
        if name in ("caffeinate",):
            continue
        print("  {:<12} {}".format(name, "up" if state.get(name) else "DOWN"))
    print("  url          {}".format(paths.web_url()))
    print("  pins         klipper {}  moonraker {}  mainsail {}  fluidd {}".format(
        pins["klipper"], pins["moonraker"], pins["mainsail"], pins["fluidd"]))
    updates, errors = versions.check_updates()
    for e in errors:
        print("  [!] " + e)
    if updates:
        print("  updates available: " + ", ".join(
            "{} {}->{}".format(c, cur, new) for c, cur, new in updates))


def cmd_logs(args):
    log = paths.KLIPPY_LOG if args.service == "klipper" else paths.MOONRAKER_LOG
    if not log.exists():
        _die("No log yet: " + str(log))
    tail = ["tail", "-n", str(args.lines), str(log)]
    if args.follow:
        tail.insert(1, "-f")
    subprocess.call(tail)


def cmd_serial(args):
    ports = detect.scan()
    if not ports:
        print("No USB serial devices found. Connect the printer board and retry.")
        return
    for p in ports:
        desc = (" (" + p["description"] + ")") if p["description"] else ""
        print(p["path"] + desc)
    target = args.set
    if args.auto:
        if len(ports) == 1:
            target = ports[0]["path"]
        else:
            print("\n--auto: {} candidates found; pass --set explicitly".format(
                len(ports)))
    if not target:
        print("\nTo use one: maklipper serial --set /dev/cu.usbserial-XXXX "
              "(or --auto for the first above)")
        return
    if not paths.PRINTER_CFG.exists():
        _die("No printer.cfg yet. Run: maklipper setup")
    if detect.write_serial(paths.PRINTER_CFG, target):
        print("Wrote serial to " + str(paths.PRINTER_CFG))
    else:
        _die("Could not find [mcu] serial: line in " + str(paths.PRINTER_CFG))


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
    print("Updates available (apply with: maklipper update --apply):")
    for comp, cur, new in updates:
        print("  {:<10} {} -> {}".format(comp, cur, new))


def _reprovision(pins):
    if process.running() is not None:
        _die("Stack is running. Do: maklipper down  (updates need a stopped stack)")
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
    print("Updated. Restart with: maklipper down && maklipper up")


def cmd_open(args):
    webbrowser.open(paths.web_url())


def cmd_ui(args):
    pins = versions.load_pins()
    if args.name:
        if args.name not in ("mainsail", "fluidd"):
            _die("UI is 'mainsail' or 'fluidd'")
        if not (paths.WEB / args.name / "index.html").exists():
            print("[!] {} not downloaded yet — run: maklipper setup".format(
                args.name))
        pins["ui"] = args.name
        versions.save_pins(pins)
        print("Default UI: " + args.name)
    url = paths.web_url()
    print(url)
    if not args.no_open:
        webbrowser.open(url)


def cmd_presets(args):
    import shutil
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    if args.save:
        if not paths.PRINTER_CFG.exists():
            _die("No printer.cfg to save")
        dst = paths.PRESETS / (args.save + ".cfg")
        shutil.copy2(paths.PRINTER_CFG, dst)
        print("Saved preset '{}' -> {}".format(args.save, dst))
        return
    if args.use:
        src = paths.PRESETS / (args.use + ".cfg")
        if not src.exists():
            avail = [p.stem for p in paths.PRESETS.glob("*.cfg")]
            _die("No preset '{}'. Available: {}".format(
                args.use, ", ".join(avail) or "none"))
        if paths.PRINTER_CFG.exists():
            shutil.copy2(paths.PRINTER_CFG, paths.CONFIG / "printer.cfg.bak")
        shutil.copy2(src, paths.PRINTER_CFG)
        print("Preset '{}' applied (previous config kept as printer.cfg.bak)."
              .format(args.use))
        if process.running() is not None:
            print("Restart to load it: maklipper restart")
        return
    if args.import_:
        src = os.path.expanduser(args.import_)
        if not os.path.isfile(src):
            _die("File not found: " + src)
        name = args.import_name or os.path.splitext(os.path.basename(src))[0]
        dst = paths.PRESETS / (name + ".cfg")
        shutil.copy2(src, dst)
        print("Imported {} as preset '{}'".format(src, name))
        return
    presets = sorted(paths.PRESETS.glob("*.cfg"))
    if not presets:
        print("No presets yet. Save one with: maklipper presets --save NAME")
        print("(Upstream example configs are also presets: import one with")
        print(" maklipper presets --import ~/.maklipper/klipper/config/<model>.cfg)")
        return
    current = paths.PRINTER_CFG.read_text() if paths.PRINTER_CFG.exists() else ""
    for p in presets:
        mark = "*" if p.read_text() == current else " "
        print("  {} {}".format(mark, p.stem))
    print("(current marked *)")


def _klippy_console(cmd, timeout=6):
    """Send a Klipper console command (STATUS, RESTART, HELP, ...) over the
    host console PTY that klippy exposes as /tmp/printer. G-code via UIs
    goes through Moonraker instead."""
    import select
    import os as _os
    pty_path = os.environ.get("MAKLIPPER_CONSOLE", "/tmp/printer")
    if not _os.path.exists(pty_path):
        _die("No Klipper console at {} (is the stack up?)".format(pty_path))
    fd = _os.open(pty_path, _os.O_RDWR | _os.O_NONBLOCK)
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
                buf += _os.read(fd, 4096).decode(errors="replace")
            except OSError:
                break
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
        print(_json.dumps(_json.loads(out), indent=2)[:1500])
        return
    script = " ".join(args.script)
    if script.strip().upper() in ("STATUS", "RESTART", "FIRMWARE_RESTART", "HELP",
                                  "QUERY_ENDSTOPS"):
        _klippy_console(script)
        return
    url = base + "/printer/gcode/script?script=" + quote(script, safe="")
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        _die("Moonraker not reachable or script rejected: " + str(e))
    print("sent: " + script + "  (output appears in: maklipper logs)")


def cmd_selftest(args):
    env = dict(os.environ)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = repo_root
    subprocess.call([str(paths.VENV_PY), "-m", "maklipper.tests.selftest"],
                    env=env)


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
        print("[!] Not set up (run maklipper setup)")
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
    sys.exit(0 if ok else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="maklipper",
        description="Klipper on macOS: pristine upstream, one command.",
    )
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    sub.add_parser("setup", help="fetch upstream components + verify build")
    p = sub.add_parser("up", help="start klipper + moonraker + mainsail")
    p.add_argument("--open", action="store_true", help="open browser")
    sub.add_parser("down", help="stop everything")
    sub.add_parser("restart", help="stop then start")
    sub.add_parser("status", help="show state, pins, available updates")
    p = sub.add_parser("logs", help="view logs")
    p.add_argument("service", choices=("klipper", "moonraker"), default="klipper",
                   nargs="?")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p = sub.add_parser("serial", help="list USB printer ports / set [mcu] serial")
    p.add_argument("--set", metavar="PATH")
    p.add_argument("--auto", action="store_true")
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
    p.add_argument("--import", dest="import_", metavar="PATH")
    p.add_argument("--import-name", metavar="NAME")
    p = sub.add_parser("gcode", help="send gcode to Klipper or query status")
    p.add_argument("script", nargs="*")
    p.add_argument("--status", action="store_true")
    sub.add_parser("selftest", help="verify web + moonraker + klippy links")
    sub.add_parser("doctor", help="environment + build + power checks")

    args = ap.parse_args(argv)
    handlers = {
        "setup": cmd_setup, "up": cmd_up, "down": cmd_down, "restart": cmd_restart,
        "status": cmd_status, "logs": cmd_logs, "serial": cmd_serial,
        "update": cmd_update, "open": cmd_open, "doctor": cmd_doctor,
        "ui": cmd_ui, "presets": cmd_presets, "gcode": cmd_gcode,
        "selftest": cmd_selftest,
    }
    handlers[args.cmd](args)
