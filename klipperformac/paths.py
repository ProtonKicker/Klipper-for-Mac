import json
import os
from pathlib import Path


def _p(env, default):
    return Path(os.environ.get(env, default)).expanduser()


APP_HOME = _p("KLIPPERFORMAC_HOME", "~/.klipperformac")

# User-visible data folder. Resolution order: env var, settings.json
# (written by `klipperformac data --set`), then the default.
SETTINGS = APP_HOME / "settings.json"
DEFAULT_DATA = "~/Documents/Klipper for Mac"
LEGACY_DATA = "~/KlipperData"


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    APP_HOME.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    tmp.replace(SETTINGS)


def _resolve_data():
    env = os.environ.get("KLIPPERFORMAC_DATA")
    if env:
        return Path(env).expanduser()
    saved = load_settings().get("data")
    if saved:
        return Path(saved).expanduser()
    new = Path(DEFAULT_DATA).expanduser()
    old = Path(LEGACY_DATA).expanduser()
    if old.exists() and not new.exists():
        try:
            old.rename(new)
        except OSError:
            return old
    return new


WEB_PORT = int(os.environ.get("KLIPPERFORMAC_WEB_PORT", "8080"))
FLUIDD_PORT = int(os.environ.get("KLIPPERFORMAC_FLUIDD_PORT", "8081"))
MOONRAKER_PORT = int(os.environ.get("KLIPPERFORMAC_MOONRAKER_PORT", "7125"))


def set_data(path):
    """Point all data-derived paths at a new folder (relocation-safe)."""
    global DATA, CONFIG, PRESETS, PRINTER_CFG, MOONRAKER_CONF, RUN, STATE
    global LOGS, WEB, WEB_MAINSAIL, WEB_FLUIDD, API_SOCKET, KLIPPY_LOG
    global MOONRAKER_LOG
    DATA = Path(path).expanduser()
    CONFIG = DATA / "config"
    PRESETS = CONFIG / "presets"
    PRINTER_CFG = CONFIG / "printer.cfg"
    MOONRAKER_CONF = CONFIG / "moonraker.conf"
    RUN = DATA / "run"
    STATE = RUN / "state.json"
    LOGS = DATA / "logs"
    WEB = DATA / "web"
    WEB_MAINSAIL = WEB / "mainsail"
    WEB_FLUIDD = WEB / "fluidd"
    API_SOCKET = RUN / "klippy.sock"
    KLIPPY_LOG = LOGS / "klippy.log"
    MOONRAKER_LOG = LOGS / "moonraker.log"


set_data(_resolve_data())

# One-time migration from the pre-rename home (package was called "maklipper").
if not os.environ.get("KLIPPERFORMAC_HOME"):
    _legacy = Path("~/.maklipper").expanduser()
    if _legacy.exists() and not APP_HOME.exists():
        try:
            _legacy.rename(APP_HOME)
            # A moved venv keeps old absolute paths in shebangs/activate.
            _vbin = APP_HOME / "venv" / "bin"
            if _vbin.is_dir():
                for _f in _vbin.iterdir():
                    try:
                        _t = _f.read_text()
                    except (OSError, UnicodeDecodeError):
                        continue
                    if ".maklipper" in _t:
                        _f.write_text(
                            _t.replace(".maklipper", ".klipperformac"))
        except OSError:
            pass

KLIPPER = APP_HOME / "klipper"
MOONRAKER = APP_HOME / "moonraker"
VENV_PY = APP_HOME / "venv" / "bin" / "python"
LOCKFILE = APP_HOME / "lockfile.json"


def ui_port(ui):
    return WEB_PORT if ui == "mainsail" else FLUIDD_PORT


def ui_url(ui):
    return "http://localhost:{}".format(ui_port(ui))


def web_url():
    ui = "fluidd"
    try:
        import json
        if LOCKFILE.exists():
            ui = json.loads(LOCKFILE.read_text()).get("ui") or "fluidd"
    except Exception:
        pass
    return ui_url(ui)


def ensure_dirs():
    for d in (APP_HOME, DATA, CONFIG, PRESETS, RUN, LOGS, WEB):
        d.mkdir(parents=True, exist_ok=True)
