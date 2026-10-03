import os
from pathlib import Path


def _p(env, default):
    return Path(os.environ.get(env, default)).expanduser()


APP_HOME = _p("MAKLIPPER_HOME", "~/.maklipper")
DATA = _p("MAKLIPPER_DATA", "~/KlipperData")

KLIPPER = APP_HOME / "klipper"
MOONRAKER = APP_HOME / "moonraker"
VENV_PY = APP_HOME / "venv" / "bin" / "python"
WEB = APP_HOME / "web" / "mainsail"
LOCKFILE = APP_HOME / "lockfile.json"

CONFIG = DATA / "config"
PRINTER_CFG = CONFIG / "printer.cfg"
MOONRAKER_CONF = CONFIG / "moonraker.conf"
RUN = DATA / "run"
STATE = RUN / "state.json"
LOGS = DATA / "logs"

API_SOCKET = RUN / "klippy.sock"
KLIPPY_LOG = LOGS / "klippy.log"
MOONRAKER_LOG = LOGS / "moonraker.log"

WEB_PORT = int(os.environ.get("MAKLIPPER_WEB_PORT", "8080"))
MOONRAKER_PORT = int(os.environ.get("MAKLIPPER_MOONRAKER_PORT", "7125"))


def web_url():
    return "http://localhost:{}".format(WEB_PORT)


def ensure_dirs():
    for d in (APP_HOME, DATA, CONFIG, RUN, LOGS):
        d.mkdir(parents=True, exist_ok=True)
