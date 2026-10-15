import json
import os
from pathlib import Path


def _p(env, default):
    return Path(os.environ.get(env, default)).expanduser()


APP_HOME = _p("KLIPPERFORMAC_HOME", "~/.klipperformac")

# User-visible data folder. Resolution order: env var, settings.json
# (written by `klipperformac data --set`), the default inside APP_HOME
# (macOS never asks for Documents permission there), then pre-existing
# legacy folders for returning users (never moved without `data --set`).
SETTINGS = APP_HOME / "settings.json"
DEFAULT_DATA = APP_HOME / "data"
LEGACY_DATA = "~/KlipperData"
LEGACY_DOCUMENTS = "~/Documents/Klipper for Mac"


def load_settings():
    """settings.json as a dict. Anything unreadable - and any JSON root that is
    not an object (the file is shared with the SwiftUI app) - reads as "no
    settings" instead of raising, so no command can be stopped by a junk file."""
    try:
        data = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings):
    """Write settings.json write-then-replace. A blocked write (read-only home,
    prefs file replaced by a directory, root-owned sidecar) raises OSError with
    the live prefs untouched and no stray temp file left behind; the caller
    turns that into one clean line for the user."""
    APP_HOME.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
        tmp.replace(SETTINGS)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _usable_data(value):
    """The settings.json "data" value if it can be trusted, else None.
    settings.json is written by the SwiftUI app too, so a junk value (a number,
    a list, a blank or a relative path) must never decide where printer.cfg
    lives: a relative one would make the whole install depend on the folder a
    command happens to be started from."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def _resolve_data():
    env = os.environ.get("KLIPPERFORMAC_DATA")
    if env:
        return Path(env).expanduser()
    saved = _usable_data(load_settings().get("data"))
    if saved:
        return saved
    docs = Path(LEGACY_DOCUMENTS).expanduser()
    if docs.exists():
        return docs
    old = Path(LEGACY_DATA).expanduser()
    if old.exists() and not DEFAULT_DATA.exists():
        try:
            DEFAULT_DATA.parent.mkdir(parents=True, exist_ok=True)
            old.rename(DEFAULT_DATA)
        except OSError:
            return old
        return DEFAULT_DATA
    return DEFAULT_DATA


def _port(env, default):
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit("Invalid {} value: {!r} (expected a port number)".format(env, raw))
    if not 1 <= value <= 65535:
        raise SystemExit("Invalid {} value: {} (must be 1..65535)".format(env, value))
    return value


WEB_PORT = _port("KLIPPERFORMAC_WEB_PORT", 8080)
FLUIDD_PORT = _port("KLIPPERFORMAC_FLUIDD_PORT", 8081)
MOONRAKER_PORT = _port("KLIPPERFORMAC_MOONRAKER_PORT", 7125)
CAM_PORT = _port("KLIPPERFORMAC_CAM_PORT", 8082)


def set_data(path):
    """Point all data-derived paths at a new folder (relocation-safe)."""
    global DATA, CONFIG, PRESETS, PRINTER_CFG, MOONRAKER_CONF, RUN, STATE
    global LOGS, WEB, WEB_MAINSAIL, WEB_FLUIDD, API_SOCKET, KLIPPY_LOG
    global MOONRAKER_LOG, CAM_STATE, CAM_LOG, TIMELAPSE
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
    CAM_STATE = RUN / "cam.json"
    CAM_LOG = LOGS / "klippacam.log"
    TIMELAPSE = DATA / "timelapse"


# One-time migration from the pre-rename home (package was called "maklipper").
# This runs BEFORE the data folder is resolved on purpose: resolving the data
# first can create ~/.klipperformac (a ~/KlipperData rename), which would make
# the old home look already migrated and strand it there. Only a real folder is
# migrated - a stray ~/.maklipper file is nobody's application home.
if not os.environ.get("KLIPPERFORMAC_HOME"):
    _legacy = Path("~/.maklipper").expanduser()
    if _legacy.is_dir() and not APP_HOME.exists():
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

set_data(_resolve_data())

KLIPPER = APP_HOME / "klipper"
MOONRAKER = APP_HOME / "moonraker"
VENV_PY = APP_HOME / "venv" / "bin" / "python"
# Shaper-only interpreter (Python 3.14): Klipper imports numpy in-process for
# SHAPER_CALIBRATE, so klippy runs here when the printer.cfg needs it.
SHAPER_VENV = APP_HOME / "venv-shaper"
SHAPER_PY = SHAPER_VENV / "bin" / "python"
BIN = APP_HOME / "bin"
CAM_BIN = BIN / "KlipperCam"
LOCKFILE = APP_HOME / "lockfile.json"


def cam_settings():
    """Camera prefs from settings.json: device/width/height/fps/quality."""
    cam = load_settings().get("cam")
    return dict(cam) if isinstance(cam, dict) else {}


def save_cam_settings(**kwargs):
    settings = load_settings()
    cam = settings.get("cam")
    cam = dict(cam) if isinstance(cam, dict) else {}
    for key, value in kwargs.items():
        if value is None:
            cam.pop(key, None)
        else:
            cam[key] = value
    settings["cam"] = cam
    save_settings(settings)
    return cam


def lan_enabled():
    return bool(load_settings().get("lan"))


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
        try:
            d.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            # exist_ok only covers the folder case: a regular file sitting on
            # one of these paths is a one-line problem, never a traceback.
            raise SystemExit(
                "data folder path is a file: {} (move or delete that file, or "
                "point at another folder: klipperformac data --set PATH)"
                .format(d))
