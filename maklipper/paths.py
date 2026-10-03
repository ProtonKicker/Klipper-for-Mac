import os
from pathlib import Path


def _p(env, default):
    return Path(os.environ.get(env, default)).expanduser()


APP_HOME = _p("MAKLIPPER_HOME", "~/.maklipper")
DATA = _p("MAKLIPPER_DATA", "~/KlipperData")

KLIPPER = APP_HOME / "klipper"
MOONRAKER = APP_HOME / "moonraker"
VENV_PY = APP_HOME / "venv" / "bin" / "python"
WEB = DATA / "web"
WEB_MAINSAIL = WEB / "mainsail"
WEB_FLUIDD = WEB / "fluidd"
LOCKFILE = APP_HOME / "lockfile.json"

CONFIG = DATA / "config"
PRESETS = CONFIG / "presets"
PRINTER_CFG = CONFIG / "printer.cfg"
MOONRAKER_CONF = CONFIG / "moonraker.conf"
RUN = DATA / "run"
STATE = RUN / "state.json"
LOGS = DATA / "logs"

API_SOCKET = RUN / "klippy.sock"
KLIPPY_LOG = LOGS / "klippy.log"
MOONRAKER_LOG = LOGS / "moonraker.log"

WEB_PORT = int(os.environ.get("MAKLIPPER_WEB_PORT", "8080"))
FLUIDD_PORT = int(os.environ.get("MAKLIPPER_FLUIDD_PORT", "8081"))
MOONRAKER_PORT = int(os.environ.get("MAKLIPPER_MOONRAKER_PORT", "7125"))


def ui_port(ui):
    return WEB_PORT if ui == "mainsail" else FLUIDD_PORT


def ui_url(ui):
    return "http://localhost:{}".format(ui_port(ui))


def web_url():
    ui = "mainsail"
    try:
        import json
        if LOCKFILE.exists():
            ui = json.loads(LOCKFILE.read_text()).get("ui") or "mainsail"
    except Exception:
        pass
    return ui_url(ui)


def ensure_dirs():
    for d in (APP_HOME, DATA, CONFIG, PRESETS, RUN, LOGS, WEB):
        d.mkdir(parents=True, exist_ok=True)
