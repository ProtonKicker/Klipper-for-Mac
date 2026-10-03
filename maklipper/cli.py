"""maklipper command-line interface: setup | up | down | status | logs |
serial | update | open | doctor."""
import argparse
import os
import subprocess
import sys
import webbrowser

from . import detect, installer, paths, process, versions
from .compat import build_env


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
        print("  Mainsail: " + paths.web_url())
        print("  Moonraker: http://localhost:{}".format(paths.MOONRAKER_PORT))
        print("  Logs: maklipper logs   Stop: maklipper down")
        if args.open:
            webbrowser.open(paths.web_url())


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
        print("  {:<10} {}".format(name, "up" if state.get(name) else "DOWN"))
    print("  url        {}".format(paths.web_url()))
    print("  pins       klipper {}  moonraker {}  mainsail {}".format(
        pins["klipper"], pins["moonraker"], pins["mainsail"]))
    updates = versions.check_updates()
    if updates:
        print("  updates    available: " + ", ".join(
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
    target = args.set or (ports[0]["path"] if args.auto else None)
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
    updates = versions.check_updates()
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
    print("Re-fetching from upstream...")
    for comp in ("klipper", "moonraker"):
        force = pins[comp] in ("master", "main")
        sha = installer.checkout(comp, pins[comp], force=force)
        pins[comp + "_sha"] = sha
        print("  {} @ {} ({})".format(comp, pins[comp], sha))
    installer.create_venv()
    installer.install_requirements()
    installer.fetch_mainsail(pins["mainsail"])
    versions.save_pins(pins)
    installer.test_chelper_build()
    print("Updated. Restart with: maklipper down && maklipper up")


def cmd_open(args):
    webbrowser.open(paths.web_url())


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
    else:
        print("[!] Not set up (run maklipper setup)")
        ok = False
    batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True).stdout.decode()
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
    sub.add_parser("doctor", help="environment + build + power checks")

    args = ap.parse_args(argv)
    handlers = {
        "setup": cmd_setup, "up": cmd_up, "down": cmd_down, "restart": cmd_restart,
        "status": cmd_status, "logs": cmd_logs, "serial": cmd_serial,
        "update": cmd_update, "open": cmd_open, "doctor": cmd_doctor,
    }
    handlers[args.cmd](args)
