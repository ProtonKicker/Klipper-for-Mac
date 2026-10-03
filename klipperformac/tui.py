"""Interactive single-instance manager (curses TUI), in the spirit of the
`instances` app. Launched by `klipperformac` with no arguments.

Terminal-adaptive styling only: default colors, bold, standout — nothing
that assumes a dark or light palette.
"""
import curses
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import webbrowser

from . import configgen, detect, paths, process, versions
from .cli import relocate_data
from .installer import (checkout, create_venv, fetch_fluidd, fetch_mainsail,
                        install_requirements, test_chelper_build)


def moonraker_objects():
    url = ("http://localhost:{}/printer/objects/query?print_stats&extruder"
           "&heater_bed&gcode_move").format(paths.MOONRAKER_PORT)
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.loads(r.read().decode())["result"]["status"]
    except Exception:
        return None


def _fmt_temp(vals):
    if vals is None:
        return "-- / --"
    return "{:.0f} / {:.0f}".format(vals[0], vals[1])


def _add_keys(std, y, text):
    """Draw a menu line with the '[k]' shortcut tokens bolded."""
    x = 2
    i = 0
    for m in re.finditer(r"\[[a-z]\]", text):
        try:
            if m.start() > i:
                std.addstr(y, x, text[i:m.start()])
                x += m.start() - i
            std.addstr(y, x, m.group(0), curses.A_BOLD)
            x += 3
            i = m.end()
        except curses.error:
            return
    try:
        std.addstr(y, x, text[i:])
    except curses.error:
        pass


def user_presets():
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in paths.PRESETS.glob("*.cfg"))


def catalog_templates():
    cdir = paths.KLIPPER / "config"
    if not cdir.exists():
        return []
    return sorted(cdir.rglob("*.cfg"))


class App(object):
    def __init__(self):
        self._message = ""
        self._msg_at = 0.0
        self.mode = "main"
        self.items = []
        self.sel = 0
        self.filter = ""
        self.save_name = ""
        self.data_input = ""
        self.footnote = ""

    # Transient: status-line messages fade out so they don't masquerade as
    # current state (e.g. "restarting..." lingering after the restart).
    @property
    def message(self):
        return self._message

    @message.setter
    def message(self, value):
        if value != self._message:
            self._msg_at = time.time()
        self._message = value

    # ---- actions -------------------------------------------------------
    def do(self, action, arg=None):
        try:
            if action == "start":
                self.message = ("starting..." if process.start()
                                else "already running")
            elif action == "stop":
                self.message = "stopped" if process.stop() else "was not running"
            elif action == "restart":
                process.stop()
                process.start()
                self.message = "restarting..."
            elif action == "open":
                webbrowser.open(paths.web_url())
                self.message = "opened " + paths.web_url()
            elif action == "toggle_ui":
                pins = versions.load_pins()
                pins["ui"] = "fluidd" if pins.get("ui") != "fluidd" else "mainsail"
                versions.save_pins(pins)
                self.message = "default UI: " + pins["ui"]
            elif action == "toggle_lan":
                settings = paths.load_settings()
                settings["lan"] = not paths.lan_enabled()
                paths.save_settings(settings)
                configgen.apply_lan(settings["lan"])
                self.message = ("LAN {} — [r] restart to apply".format(
                    "on" if settings["lan"] else "off"))
            elif action == "serial":
                ports = detect.scan()
                if not ports:
                    self.message = "no USB serial devices found"
                else:
                    self.items = [p["path"] for p in ports]
                    self.mode = "serial"
                    self.sel = 0
                    self.footnote = "[enter] use device"
            elif action == "presets":
                self.filter = ""
                self.items = user_presets()
                self.mode = "presets"
                self.sel = 0
                self.footnote = ("[enter] use   [t] templates   "
                                 "[s] save current   [type] filter   [esc] back")
            elif action == "templates":
                self.filter = ""
                self.items = [str(p) for p in catalog_templates()]
                self.mode = "templates"
                self.sel = 0
                self.footnote = ("[enter] import as preset   [type] filter   "
                                 "[esc] back")
            elif action == "save_preset":
                self.save_name = ""
                self.mode = "name"
                self.footnote = "preset name, [enter] saves, [esc] cancels"
            elif action == "data":
                self.data_input = ""
                self.mode = "data"
                self.footnote = "[enter] move   [esc] cancel"
            elif action == "logs":
                log = paths.KLIPPY_LOG if arg == "klipper" else paths.MOONRAKER_LOG
                if log.exists():
                    tail = subprocess.run(["tail", "-n", "18", str(log)],
                                          capture_output=True).stdout.decode()
                    self.items = tail.splitlines()
                    self.mode = "logs"
                    self.footnote = "[esc] back"
                else:
                    self.message = "no log yet"
            elif action == "update":
                updates, errors = versions.check_updates()
                if errors:
                    self.message = "; ".join(errors)[:60]
                elif updates:
                    self.items = ["{}: {} -> {}".format(c, a, b)
                                  for c, a, b in updates]
                    self.mode = "updates"
                    self.footnote = "[a] apply"
                else:
                    self.message = "all components up to date"
            elif action == "apply_updates":
                pins = versions.load_pins()
                updates, _ = versions.check_updates()
                for comp, _, new in updates:
                    if comp == "klipper" and pins[comp] in ("master", "main"):
                        continue
                    pins[comp] = new
                if process.running() is not None:
                    self.message = "stop stack before updating"
                else:
                    for comp in ("klipper", "moonraker"):
                        force = pins[comp] in ("master", "main")
                        pins[comp + "_sha"] = checkout(comp, pins[comp],
                                                       force=force)
                    create_venv()
                    install_requirements()
                    fetch_mainsail(pins["mainsail"])
                    fetch_fluidd(pins["fluidd"])
                    versions.save_pins(pins)
                    test_chelper_build()
                    self.message = "updated; [s] start when ready"
            elif action in ("back", "quit"):
                self.mode = "main"
                self.filter = ""
                self.footnote = ""
        except SystemExit as e:
            self.message = str(e)[:70]
        except Exception as e:
            self.message = "error: " + str(e)[:70]

    # ---- derived -------------------------------------------------------
    def visible(self):
        if self.filter:
            f = self.filter.lower()
            return [i for i in self.items if f in os.path.basename(i).lower()]
        return self.items

    # ---- rendering -----------------------------------------------------
    def draw(self, std):
        h, w = std.getmaxyx()
        std.erase()

        def safe(y, x, text, attr=0):
            # Off-screen writes raise curses.error, which would escape
            # curses.wrapper and kill the app (looks like a terminal restart).
            if 0 <= y < h and 0 <= x < w:
                try:
                    std.addstr(y, x, text[:max(0, w - x - 1)], attr)
                except curses.error:
                    pass

        def row(y, label, text, attr=0):
            safe(y, 2, label, curses.A_BOLD)
            safe(y, 12, text, attr)

        safe(0, 2, "Klipper for Mac", curses.A_BOLD)
        state = process.running()
        if state is None:
            row(2, "stack", "STOPPED   [s] start")
        else:
            ups = [n for n, ok in state.items() if ok]
            flag = "RUNNING" if ups else "not responding [r]"
            row(2, "stack", "{}   {}".format(flag, ", ".join(ups)))
        obj = moonraker_objects()
        if obj:
            ps = obj.get("print_stats", {})
            msg = (ps.get("message") or "").splitlines()
            printer = ps.get("state", "?")
            if printer.lower() in ("error", "startup", "paused"):
                printer += " - " + (msg[0] if msg else "")
            row(3, "printer", "{}{}".format(
                printer, (" | file: " + ps["filename"]) if ps.get("filename")
                else ""))
            ex = obj.get("extruder") or {}
            hb = obj.get("heater_bed") or {}
            gmv = (obj.get("gcode_move") or {}).get("gcode_position")
            row(4, "hotend", "{}      bed  {}".format(
                _fmt_temp((ex.get("temperature", 0), ex.get("target", 0))),
                _fmt_temp((hb.get("temperature", 0), hb.get("target", 0)))))
            if isinstance(gmv, list) and len(gmv) >= 3:
                row(5, "position",
                    "X {:.1f}   Y {:.1f}   Z {:.2f}".format(
                        gmv[0], gmv[1], gmv[2]))
        else:
            row(3, "printer", "(moonraker not answering)")
        pins = versions.load_pins()
        row(7, "versions", "klipper {}   moonraker {}   ui {}".format(
            pins.get("klipper_sha", "")[:7] or pins["klipper"],
            pins["moonraker"], pins.get("ui", "fluidd")))
        row(8, "data", str(paths.DATA))
        ui = pins.get("ui", "fluidd")
        api_host = "localhost"
        if paths.lan_enabled():
            ip = detect.lan_ip()
            if ip:
                lan_line = ("open  {} http://{}:{}   [a] close"
                            .format(ui, ip, paths.ui_port(ui)))
                api_host = ip
            else:
                lan_line = ("open, but this Mac has no network address"
                            "   [a] close")
        else:
            lan_line = "closed (this Mac only)   [a] open to local network"
        row(10, "lan", lan_line)
        row(11, "api", "http://{}:{}".format(api_host, paths.MOONRAKER_PORT))
        y = 13
        if self.mode == "main":
            col = lambda s: s.ljust(24)
            lines = [
                col("[s] start") + col("[o] open dashboard") + "[w] switch UI",
                col("[x] stop") + col("[r] restart") + "[q] quit",
                "",
                col("[d] serial device") + col("[p] presets") + "[f] data folder",
                col("[l] klipper logs") + col("[m] moonraker logs") + "[u] updates",
                col("[a] lan access") + ("open" if paths.lan_enabled() else "closed"),
            ]
            for line in lines:
                if line:
                    _add_keys(std, y, line[:w - 3])
                y += 1
        else:
            label = self.mode
            vis = self.visible()
            head = "── {} ({}) ──".format(label, len(vis))
            if self.filter:
                head += "  filter: {}".format(self.filter)
            if self.mode == "name":
                head = "── save preset: {}_".format(self.save_name)
            if self.mode == "data":
                head = "── move data folder ──"
            safe(y, 2, head, curses.A_BOLD)
            y += 1
            if self.mode == "data":
                safe(y, 2, "current: {}".format(paths.DATA))
                safe(y + 1, 2, "new: {}_".format(self.data_input),
                     curses.A_BOLD)
                safe(y + 2, 2, "type a path — [enter] moves everything, "
                               "[esc] cancels")
            elif self.mode == "name":
                pass
            else:
                height = h - y - 3
                if height > 0:
                    if self.sel >= len(vis):
                        self.sel = max(0, len(vis) - 1)
                    start = max(0,
                                min(self.sel - height // 2, len(vis) - height))
                    for i, item in enumerate(vis[start:start + height]):
                        attr = curses.A_REVERSE if start + i == self.sel else 0
                        text = os.path.basename(item)
                        if self.mode == "serial":
                            text += "   " + detect._describe(item)
                        safe(y + i, 2, (" > " if attr else "   ") + text, attr)
                    if self.footnote:
                        safe(h - 3, 2, self.footnote)
        if self._message and time.time() - self._msg_at > 8:
            self._message = ""
        if self.message:
            safe(h - 1, 1, self.message)

    # ---- input ---------------------------------------------------------
    def key(self, ch):
        if self.mode == "main":
            table = {"s": "start", "x": "stop", "r": "restart", "o": "open",
                     "w": "toggle_ui", "d": "serial", "p": "presets",
                     "f": "data", "a": "toggle_lan",
                     "l": ("logs", "klipper"), "m": ("logs", "moonraker"),
                     "u": "update"}
            c = chr(ch) if ch > 0 else ""
            if c in table:
                act = table[c]
                if isinstance(act, tuple):
                    self.do(act[0], act[1])
                else:
                    self.do(act)
                return None
            return "quit" if c in ("q", "\x1b") else None
        if self.mode == "name":
            return self.key_name(ch)
        if self.mode == "data":
            return self.key_data(ch)
        c = chr(ch) if ch > 0 else ""
        if ch == curses.KEY_UP or c == "k":
            self.sel = max(0, self.sel - 1)
            return None
        if ch == curses.KEY_DOWN or c == "j":
            self.sel = min(max(0, len(self.visible()) - 1), self.sel + 1)
            return None
        if ch in (10, 13):
            return self.key_enter() or None
        if ch in (27, 8, 127) and self.mode != "logs":
            if self.filter and ch != 27:
                self.filter = self.filter[:-1]
                self.sel = 0
                return None
            self.do("back")
            return None
        if self.mode == "logs" and ch in (27, ord("q")):
            self.do("back")
            return None
        if self.mode == "updates" and c == "a":
            self.do("apply_updates")
            self.do("back")
            return None
        if self.mode == "presets" and c == "t":
            self.do("templates")
            return None
        if self.mode == "presets" and c == "s":
            self.do("save_preset")
            return None
        if 32 <= ch < 127:
            self.filter += c
            self.sel = 0
            return None
        return None

    def key_name(self, ch):
        if ch == 27:
            self.do("back")
            return None
        if ch in (10, 13):
            name = self.save_name.strip().replace(" ", "-")
            if name and paths.PRINTER_CFG.exists():
                shutil.copy2(paths.PRINTER_CFG, paths.PRESETS / (name + ".cfg"))
                self.message = "saved preset '{}'".format(name)
            else:
                self.message = "nothing to save"
            self.do("back")
            return None
        if ch in (8, 127):
            self.save_name = self.save_name[:-1]
            return None
        if 32 <= ch < 127:
            self.save_name += chr(ch)
        return None

    def key_data(self, ch):
        if ch == 27:
            self.do("back")
            return None
        if ch in (10, 13):
            path = self.data_input.strip()
            if not path:
                self.message = "no path typed — cancelled"
            else:
                try:
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        result = relocate_data(path)
                    extras = [l for l in buf.getvalue().splitlines() if l.strip()]
                    self.message = result + (("  |  " + "; ".join(extras))
                                             if extras else "")
                except Exception as e:
                    self.message = "error: " + str(e)[:70]
            self.do("back")
            return None
        if ch in (8, 127):
            self.data_input = self.data_input[:-1]
            return None
        if 32 <= ch < 127:
            self.data_input += chr(ch)
        return None

    def key_enter(self):
        vis = self.visible()
        if not vis or self.sel >= len(vis):
            return None
        chosen = vis[self.sel]
        if self.mode == "serial":
            if not paths.PRINTER_CFG.exists():
                self.message = "no printer.cfg (run: klipperformac setup)"
            elif detect.write_serial(paths.PRINTER_CFG, chosen):
                self.message = "serial set — [r] restart to apply"
            else:
                self.message = "no [mcu] section in printer.cfg"
            self.do("back")
        elif self.mode == "presets":
            self._apply_preset(paths.PRESETS / (chosen + ".cfg"), chosen)
        elif self.mode == "templates":
            name = os.path.splitext(os.path.basename(chosen))[0]
            shutil.copy2(chosen, paths.PRESETS / (name + ".cfg"))
            self._apply_preset(paths.PRESETS / (name + ".cfg"), name)

    def _apply_preset(self, src, name):
        if paths.PRINTER_CFG.exists():
            shutil.copy2(paths.PRINTER_CFG, paths.CONFIG / "printer.cfg.bak")
        shutil.copy2(src, paths.PRINTER_CFG)
        self.message = "preset '{}' applied — [r] restart to load".format(name)
        self.do("back")


def run(std):
    curses.curs_set(0)
    std.keypad(True)
    std.timeout(1000)
    # Terminal-adaptive: no color pairs of our own; -1 = the terminal's own
    # foreground/background (keeps transparency & themes intact).
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        pass
    app = App()
    while True:
        app.draw(std)
        try:
            ch = std.getch()
        except curses.error:
            continue
        if ch == -1:
            continue
        if app.key(ch) == "quit":
            return
        time.sleep(0.01)


def main():
    if not (os.isatty(0) and os.isatty(1)):
        raise SystemExit("Interactive dashboard needs a terminal; "
                         "use klipperformac <command> (see -h).")
    curses.wrapper(run)
