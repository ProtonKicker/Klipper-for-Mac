import json
import os
import math
from pathlib import Path

# Klipper/Moonraker configs are routinely saved by editors in cp1252 or
# Latin-1 (German comments in vendor configs), and Moonraker itself does not
# require UTF-8. latin-1 is the last resort because it is byte-lossless: every
# byte round-trips, so a rewrite never mangles a comment we did not touch.
CFG_FALLBACK_ENCODING = "latin-1"
_UTF8_BOM = b"\xef\xbb\xbf"
_UNREADABLE_SETTINGS = object()


def _env_dir(env, default, what):
    """A folder from the environment, always absolute.

    `export KLIPPERFORMAC_HOME=` (empty) means "unset", never "the folder I am
    standing in": Path('') is '.', which would make the whole install - and
    `klipperformac uninstall` - depend on the current directory. A relative
    path is refused the same way, because the stack is started from a different
    folder than the terminal that typed the command."""
    raw = os.environ.get(env)
    if raw is None or not raw.strip():
        raw = default
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise SystemExit("Invalid {} value: {!r} - {} must be an absolute path "
                         "(starting with / or ~), otherwise every command "
                         "depends on the folder it happens to be run from"
                         .format(env, raw, what))
    return path


APP_HOME = _env_dir("KLIPPERFORMAC_HOME", "~/.klipperformac",
                    "the Klipper-for-Mac app folder")
if APP_HOME == Path.home():
    raise SystemExit("Invalid KLIPPERFORMAC_HOME value: {} is your home "
                     "folder. The app folder must be a folder of its own, e.g. "
                     "~/.klipperformac".format(APP_HOME))

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


def _load_settings_for_update():
    """settings.json for a mutating write path.

    Missing file -> {}. A syntactically valid non-object root also behaves like
    {} (matching load_settings()). But a torn or unreadable file is preserved in
    place: mutating commands must not silently replace a file they could not
    faithfully read."""
    try:
        raw = SETTINGS.read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        if SETTINGS.is_file():
            return _UNREADABLE_SETTINGS
        raise
    try:
        data = json.loads(raw)
    except ValueError:
        bak = SETTINGS.with_suffix(".json.bak")
        try:
            bak.write_text(raw)
        except OSError:
            pass
        return {}
    return data if isinstance(data, dict) else {}


def _json_safe(value):
    """Recursively map non-finite floats to None so settings.json stays strict."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def save_settings(settings):
    """Write settings.json write-then-replace. A blocked write (read-only home,
    prefs file replaced by a directory, root-owned sidecar) raises OSError with
    the live prefs untouched and no stray temp file left behind; the caller
    turns that into one clean line for the user."""
    APP_HOME.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".json.tmp")
    try:
        payload = json.dumps(_json_safe(settings), indent=2, sort_keys=True,
                             allow_nan=False) + "\n"
        tmp.write_text(payload)
        tmp.replace(SETTINGS)
    except (OSError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_config(path):
    """(text, encoding) of a user-editable config file.

    Never raises UnicodeDecodeError: one non-UTF-8 byte in a comment must not
    be able to stop `up`/`doctor`/`lan`/`presets`. OSError (missing/unreadable)
    is the caller's business. The encoding comes back so a rewrite writes the
    bytes the user had with only our line changed - which is why utf-8 is tried
    as plain utf-8 and "utf-8-sig" is only reported when a BOM is really there
    (encoding a BOM-less file as utf-8-sig would add one)."""
    data = Path(path).read_bytes()
    if data.startswith(_UTF8_BOM):
        try:
            # Decoded as utf-8-sig so the BOM leaves the text, and reported as
            # utf-8-sig so the rewrite puts exactly one back.
            return data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode(CFG_FALLBACK_ENCODING), CFG_FALLBACK_ENCODING


def read_config_text(path):
    """The text of a config file, byte-safe (see read_config)."""
    return read_config(path)[0]


def write_config(path, text, encoding="utf-8"):
    """Write-then-replace for any file a running stack reads (printer.cfg,
    moonraker.conf, state/settings json).

    Writing in place truncates first, so a crash, a full disk or a killed
    process mid-write used to leave a half config behind - which Klipper/Moonraker
    then read as a broken install. The temp file sits in the same folder (same
    filesystem) so os.replace is atomic, and it carries the encoding we read."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
        os.replace(tmp, path)
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
    if not path.is_absolute() or protected_data_dir(path):
        return None
    return path


def protected_data_dir(path):
    """Folders that must never become (or be deleted as) the data folder: the
    home root, a filesystem root, and the top-level user folders people keep
    irreplaceable files in. `data --set` and `uninstall --data` both consult
    this, so one typo cannot turn a housekeeping command into a wipe."""
    if path in (Path.home(), Path("/")) or path.parent == path:
        return True
    home = Path.home()
    for name in ("Documents", "Desktop", "Downloads", "Pictures", "Movies",
                 "Music", "Library"):
        if path == home / name:
            return True
    return False


def looks_like_data_dir(path):
    """Does `path` hold the layout a Klipper-for-Mac data folder has?

    A directory is only ever deleted after this question is answered yes: the
    alternative is `uninstall --data` wiping whatever folder a stale settings
    value happens to name."""
    path = Path(path)
    if not path.is_dir():
        return False
    if (path / "config" / "printer.cfg").is_file():
        return True
    known = ("config", "run", "logs", "gcodes", "web", "timelapse")
    return sum(1 for name in known if (path / name).is_dir()) >= 2


def looks_like_app_home(path):
    """Does `path` hold an install this tool made?

    Same purpose as looks_like_data_dir, one level up: `uninstall` removes a
    whole tree. Only markers a SETUP actually writes count - deliberately not
    `data/` or `bin/`, because our own commands create those on the way (taking
    the run/cli.lock makes run/), and a folder that only holds what this very
    command just made is not evidence of an installation."""
    path = Path(path)
    if not path.is_dir():
        return False
    for marker in ("lockfile.json", "src", "shims", "refs.d"):
        if (path / marker).exists():
            return True
    for folder in ("venv", "venv-shaper", "klipper", "moonraker"):
        if (path / folder).is_dir():
            return True
    return False


def _resolve_data():
    env = os.environ.get("KLIPPERFORMAC_DATA")
    if env is not None and env.strip():
        path = _env_dir("KLIPPERFORMAC_DATA", None, "the data folder")
        if protected_data_dir(path):
            raise SystemExit("Invalid KLIPPERFORMAC_DATA value: {} is too "
                             "important a folder to hold printer data. Pick a "
                             "folder of its own, e.g. {}"
                             .format(path, DEFAULT_DATA))
        return path
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
    global MOONRAKER_LOG, CAM_STATE, CAM_LOG, TIMELAPSE, CLI_LOCK
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
    CLI_LOCK = RUN / "cli.lock"


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
    settings = _load_settings_for_update()
    if settings is _UNREADABLE_SETTINGS:
        cam = load_settings().get("cam")
        return dict(cam) if isinstance(cam, dict) else {}
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


def default_ui():
    """The UI this install actually serves: the lockfile records what setup
    fetched, "fluidd" is the shipped default. Anything that names the UI to a
    user (selftest labels, `open`) reads it from here, so a label cannot lie
    about which folder answered."""
    ui = "fluidd"
    try:
        if LOCKFILE.exists():
            ui = json.loads(read_config_text(LOCKFILE)).get("ui") or "fluidd"
    except Exception:
        pass
    return ui if ui in ("mainsail", "fluidd") else "fluidd"


def web_url():
    return ui_url(default_ui())


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
