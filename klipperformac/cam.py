"""The Mac camera (KlipperCam helper), independent of the Klipper stack.

Privacy first: nothing is captured until the user turns the camera on, the
stream is loopback-only, and Moonraker only learns about it through a managed
[webcam maccam] section. Supervision mirrors process.py: a JSON state file
re-validated on every read (pid + cmdline marker + boot time), so a crash or
a reboot self-heals on the next command. The helper itself is a Swift binary
owned by the app; this module only spawns, tracks and talks to it.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import configgen, paths

CAM = "KlipperCam"
# audio/mic are opt-in and deliberately NOT defaults: prefs() (and everything
# the app reads out of it) must look exactly like it did before video existed.
DEFAULT_PREFS = {"width": 1280, "height": 720, "fps": 15, "quality": 80}
PREF_KEYS = ("width", "height", "fps", "quality", "device", "audio", "mic")
# Helper --check-permission exit codes -> (word, usable).
PERMISSIONS = {0: ("authorized", True), 2: ("denied", False),
               3: ("restricted", False), 4: ("not determined", False)}
GUI_BUNDLES = (
    "/Applications/Klipper for Mac.app/Contents/Resources/" + CAM,
    "~/Applications/Klipper for Mac.app/Contents/Resources/" + CAM,
)
LOG_WARN_BYTES = 50 * 1024 * 1024
# Single canonical spelling, matching paths.py and the app: KLIPPERFORMAC_*.
CAM_BIN_ENVS = ("KLIPPERFORMAC_CAM_BIN",)
REPO_DIR_ENVS = ("KLIPPERFORMAC_REPO_DIR",)
IDENTIFIER = "com.klipperformac.klippercam"
MIN_TARGET = "apple-macos13.0"
# Every subprocess call is bounded: a hung tool must not hang a user command.
SWIFT_TIMEOUT = 900
COMPILE_TIMEOUT = 600
SIGN_TIMEOUT = 60
CMD_TIMEOUT = 10
# --- video recording (the streaming helper owns the lens; see CONTRACT below)
# One AVCaptureSession per camera: a second helper cannot open the lens while a
# stream is up, so a video is always written by THE camera process - started (or
# restarted) with its own boot flags `--record PATH [--seconds N] [--mic [NAME]]`
# and watched over its loopback status port. Never a second capture, never a
# shell, never an ffmpeg pass afterwards.
RECORDINGS_NAME = "recordings"
RECORD_SUFFIX = ".mp4"
RECORD_AUDIO_SUFFIX = ".m4a"
RECORD_TIME_FMT = "%Y-%m-%d_%H%M%S"
RECORD_MIN_SECS = 1
RECORD_MAX_SECS = 3600            # a forgotten `cam record` must not fill a disk
RECORD_MIN_FREE_MB = 500          # refuse to start on a nearly full volume
RECORD_CAP_MB = 2048              # our own recordings may be pruned past this
MB = 1024 * 1024
RECORD_TIMEOUT = 6                # the read-only status GET, not the recording
RECORD_WAIT = 30                  # the helper allows 20s to close a movie
RECORD_POLL = 0.5                 # how often a bounded wait knocks
START_SETTLE = 0.6                # long enough to notice a helper that dies now
# A filename we wrote, and only one we wrote: prune() never touches a file the
# user made by hand, whatever it is called.
RECORD_NAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{6}(-[A-Za-z0-9][A-Za-z0-9._-]{0,63})?\.(mp4|m4a)$")
RECORD_ENVS = ("KLIPPERFORMAC_RECORDINGS_MAX_MB",)
FREE_ENVS = ("KLIPPERFORMAC_RECORD_MIN_FREE_MB",)
# Flags a record-aware helper advertises in its own --help. ANY of them counts.
# This probe is the only thing that decides whether a flag may be handed over at
# all: an OLD helper exits 1 on an option it does not know, which would take the
# camera down along with the recording, so it is never tried and pretended about.
RECORD_MARKERS = ("--record", "--seconds", "--mic", "--list-mics",
                  "--check-mic-permission")
# What `--mic`, `cam mics` and `cam mic-permission` need to see in that --help.
MIC_MARKERS = ("--mic", "--list-mics", "--check-mic-permission")
# The helper's own exit codes, from its contract: 0 a complete file, 3 nothing
# kept (never a broken one), 1 bad usage or the camera permission is missing.
RECORD_OK, RECORD_NO_FILE, RECORD_REFUSED = 0, 3, 1
# Helper --check-mic-permission exit codes -> the exactly-one word it prints.
# Kept apart from PERMISSIONS above: the microphone spells its fourth word
# "notDetermined" (no space) and it never asks for anything.
MIC_PERMISSIONS = {0: "authorized", 2: "denied", 3: "restricted",
                   4: "notDetermined"}
MIC_OFF_WORDS = ("off", "no", "false", "0", "none")
MIC_ON_WORDS = ("on", "yes", "true", "1")
# The helper's stdout lines that carry facts Python must not invent. The close
# line is the recording measured by the thing that wrote it (length, bytes,
# sound); "Recording to ..." is the open line and `KlipperCam: ...` on stderr
# carries the refusals and the degraded paths (e.g. "No sound: ...").
RECORD_OPEN_RE = re.compile(r"^Recording to (.+) \((picture only|with sound)\)$")
RECORD_DONE_RE = re.compile(r"^Recorded (.+) \((\d+(?:\.\d+)?) s, (\d+) bytes, "
                            r"sound (on|off)\)$")
HELPER_NOTE_RE = re.compile(r"^KlipperCam: (.+)$")


def _process():
    from . import process  # late import: process owns the shared pid helpers
    return process


def _signal(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _reap(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _alive(pid):
    return bool(pid) and str(pid) in _process()._live_cmds([pid])


def _popen(cmd, **kw):
    defaults = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
    defaults.update(kw)
    return subprocess.Popen(cmd, **defaults)


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def stream_url(port=None):
    return "http://localhost:{}/?action=stream".format(int(port or paths.CAM_PORT))


def snapshot_url(port=None):
    return "http://localhost:{}/?action=snapshot".format(int(port or paths.CAM_PORT))


def _needs_copy(src, dest):
    try:
        return (dest.stat().st_size != src.stat().st_size or
                dest.stat().st_mtime_ns < src.stat().st_mtime_ns)
    except OSError:
        return True


def helper_path():
    """The Swift camera helper, copied out of the app bundle if needed."""
    for env in CAM_BIN_ENVS:
        raw = os.environ.get(env)
        if raw:
            path = Path(raw).expanduser()
            if paths.is_file(path):
                return path
    installed = paths.CAM_BIN if paths.is_file(paths.CAM_BIN) else None
    blocked_dest = paths.path_exists(paths.CAM_BIN) and \
        not paths.is_file(paths.CAM_BIN)
    for cand in GUI_BUNDLES:
        inside = Path(cand).expanduser()
        if not paths.is_file(inside):
            continue
        if blocked_dest:
            return inside
        try:
            paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
            if installed is None or _needs_copy(inside, paths.CAM_BIN):
                shutil.copy2(str(inside), str(paths.CAM_BIN))
                os.chmod(str(paths.CAM_BIN), 0o755)
        except OSError:
            return inside  # read-only bundle etc.: use it where it is
        return paths.CAM_BIN
    if installed is not None:
        return installed
    return None


def _repo_mac_dir():
    """Dir holding KlipperMac/Package.swift (the env var may point at either
    the repo root or the KlipperMac folder), or None."""
    roots = []
    for env in REPO_DIR_ENVS:
        if os.environ.get(env):
            roots.append(Path(os.environ[env]).expanduser())
    roots += list(Path(__file__).resolve().parents[1:3])
    roots.append(Path("~/.klipperformac/src/Klipper-for-Mac").expanduser())
    for root in roots:
        for mac in (root, root / "KlipperMac"):
            if (mac / "Package.swift").is_file():
                return mac
    return None


def build_helper(verbose=True):
    """Compile the helper from this repo, install and ad-hoc sign it. Returns
    (ok, lines); never reports success it did not earn. `swift build` is tried
    first, then the direct swiftc recipe, because a Mac with only the Command
    Line Tools (the common case here) cannot run SwiftPM at all."""
    lines = []
    mac = _repo_mac_dir()
    if mac is None:
        return False, [
            "Cannot build the camera helper: no Klipper for Mac source found.",
            "Point KLIPPERFORMAC_REPO_DIR at the folder that contains "
            "KlipperMac/Package.swift, or install the app (it ships the helper)."]
    swift = shutil.which("swift")
    xcrun = shutil.which("xcrun")
    if swift is None and xcrun is None:
        return False, [
            "Cannot build the camera helper: Swift is not installed.",
            "Install the compiler tools, then try again:",
            "  xcode-select --install"]
    if verbose:
        lines.append("Building {} in {} (first build can take several minutes)..."
                     .format(CAM, mac))
    try:
        scratch = Path(tempfile.mkdtemp(prefix="klippercam-build-"))
    except OSError as e:
        return False, ["Could not make a temporary folder to build in: " + str(e)]
    built = None
    fails = []
    try:
        if swift is not None:
            built, tail = _swift_build(mac, scratch, swift)
            if built is None:
                fails += ["`swift build` said:"] + ["  " + l for l in tail]
                # Command Line Tools cannot run SwiftPM at all (no platform
                # path), which is normal - so keep going instead of stopping.
                if xcrun is not None and verbose:
                    lines.append("The package build is not usable here (it needs "
                                 "a full Xcode); compiling the sources directly.")
        if built is None and xcrun is not None:
            built, notes = _swiftc_build(mac, scratch, xcrun, verbose)
            if built is None:
                fails += ["`swiftc` said:"] + ["  " + l for l in notes]
            else:
                lines += [l for l in notes
                          if verbose or l.startswith("[!]")]
        if built is None:
            return False, lines + ["Build failed:"] + fails[-10:] + [
                "Try it by hand: cd {} && bash build-app.sh".format(mac)]
        dest = paths.CAM_BIN
        try:
            _install_helper(built, dest)
        except OSError as e:
            return False, lines + ["Built the helper but could not install it to "
                                   "{}: {}".format(dest, e)]
        lines += _sign_helper(dest)
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)
    lines.append("Camera helper ready: " + str(dest))
    return True, lines


def _tail(res, count=6):
    """Last few non-empty lines of a finished subprocess, for the user."""
    out = (res.stdout + res.stderr).decode(errors="replace").splitlines()
    return [l.strip()[:200] for l in out if l.strip()][-count:]


def _swift_build(mac, scratch, swift):
    """SwiftPM build (the Xcode path). (binary|None, output tail)."""
    cmd = [swift, "build", "-c", "release", "--product", CAM,
           "--scratch-path", str(scratch)]
    try:
        res = subprocess.run(cmd, cwd=str(mac), capture_output=True,
                             timeout=SWIFT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, ["swift build did not finish within 15 minutes."]
    except OSError as e:
        return None, ["Could not run the Swift build: " + str(e)]
    built = scratch / "release" / CAM
    if res.returncode != 0 or not built.is_file():
        return None, _tail(res)
    return built, []


def _swiftc_build(mac, scratch, xcrun, verbose):
    """Compile Sources/KlipperCam/*.swift straight with swiftc - what
    KlipperMac/build-app.sh does on a Command-Line-Tools-only Mac. The
    Info.plist is embedded in __TEXT so macOS ties the camera permission to
    KlipperCam itself instead of to the terminal that ran it. Returns
    (binary|None, output tail)."""
    srcs = sorted(str(p) for p in (mac / "Sources" / CAM).glob("*.swift"))
    if not srcs:
        return None, ["No Swift sources found in " + str(mac / "Sources" / CAM)]
    try:
        sdk = subprocess.run([xcrun, "--sdk", "macosx", "--show-sdk-path"],
                             capture_output=True,
                             timeout=CMD_TIMEOUT).stdout.decode().strip()
    except Exception:
        sdk = ""
    if not sdk or not Path(sdk).is_dir():
        return None, ["Could not find the macOS SDK. Install the compiler "
                      "tools, then try again:  xcode-select --install"]
    plist = mac / "Resources" / (CAM + "-Info.plist")
    embed = ["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
             "-Xlinker", "__info_plist", "-Xlinker", str(plist)]
    notes = []
    if not plist.is_file():
        embed = []
        notes.append("[!] No {} in this source tree, so macOS will ask for "
                     "camera permission for your terminal app instead of {}."
                     .format(plist.name, CAM))
    done = []
    fails = []
    for arch in _arches():
        out = scratch / "{}-{}".format(CAM, arch)
        cmd = ([xcrun, "swiftc", "-O", "-sdk", sdk, "-target",
                arch + "-" + MIN_TARGET] + srcs + embed + ["-o", str(out)])
        try:
            res = subprocess.run(cmd, cwd=str(mac), capture_output=True,
                                 timeout=COMPILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            fails += ["Compiling for {} took too long.".format(arch)]
            continue
        except OSError as e:
            return None, ["Could not run swiftc: " + str(e)]
        if res.returncode == 0 and out.is_file():
            done.append(out)
            if verbose:
                notes.append("Compiled {} for {}.".format(CAM, arch))
        else:
            fails += ["Compiling for {} failed:".format(arch)] + _tail(res)
    if not done:
        return None, (fails or ["swiftc produced no binary."])
    if len(done) > 1:
        fat = _lipo(done, scratch, notes)
        if fat is not None:
            return fat, notes
    return done[0], notes


def _arches():
    """This Mac plus the Apple Silicon slice, like build-app.sh (one of them
    may fail on a bare toolchain; the survivors still make a usable binary)."""
    seen = []
    for arch in (os.uname().machine, "arm64"):
        if arch and arch not in seen:
            seen.append(arch)
    return seen


def _lipo(parts, scratch, notes):
    """Fuse the per-arch binaries; None if that is not possible here."""
    lipo = shutil.which("lipo")
    if lipo is None:
        notes.append("[!] lipo is missing, so the helper is built for this Mac "
                     "only.")
        return None
    universal = scratch / (CAM + "-universal")
    cmd = [lipo, "-create"] + [str(p) for p in parts] + ["-output",
                                                          str(universal)]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=CMD_TIMEOUT)
    except Exception:
        res = None
    if res is not None and res.returncode == 0 and universal.is_file():
        notes.append("Universal binary: " +
                     " + ".join(p.name.split("-")[-1] for p in parts) + ".")
        return universal
    notes.append("[!] The two architectures could not be fused, so the helper "
                 "is built for this Mac only.")
    return None


def _install_helper(built, dest):
    """Install by rename, so a helper that is still streaming cannot block a
    rebuild and a half-copied binary is never left as the live one."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".tmp")
    try:
        shutil.copy2(str(built), str(tmp))
        os.chmod(str(tmp), 0o755)
        tmp.replace(dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _sign_helper(dest):
    """Ad-hoc sign under our own bundle identifier. An unsigned helper still
    runs, so a signing problem is a warning, never a failed build."""
    cmd = ["codesign", "--force", "--sign", "-", "--identifier", IDENTIFIER,
           str(dest)]
    try:
        sign = subprocess.run(cmd, capture_output=True, timeout=SIGN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ["[!] Could not sign the helper in time; it is installed "
                "unsigned. Run the build again to sign it."]
    except OSError as e:
        return ["[!] Could not run codesign: " + str(e)]
    if sign.returncode != 0:
        return ["[!] Installed but not signed: {} (the camera should still "
                "work; if macOS blocks it, run the build again)"
                .format((sign.stderr.decode(errors="replace").strip()
                         or "codesign failed")[-200:])]
    return ["Signed (ad-hoc) as " + IDENTIFIER + "."]


def permission():
    """(word, usable) from the helper itself; ('unknown', False) when the
    helper is missing or did not answer."""
    helper = helper_path()
    if helper is None:
        return "unknown", False
    try:
        code = subprocess.run([str(helper), "--check-permission"],
                              capture_output=True, timeout=30).returncode
    except Exception:
        return "unknown", False
    return PERMISSIONS.get(code, ("unknown", False))


# (key, set of flags) for the last helper we asked. Keyed on path+mtime+size so
# a rebuild is picked up and a stale answer can never survive an upgrade.
_CAPS_CACHE = {}
NO_RECORD = [
    "This camera helper cannot record video: it is an old build that only "
    "streams and snapshots.",
    "Update the camera helper once, then try again:",
    "  klipperformac cam build",
    "(the camera itself is unaffected - streaming and snapshots keep working)"]
NO_MIC = [
    "This camera helper knows nothing about microphones (it is an old build).",
    "Update the camera helper once, then try again:",
    "  klipperformac cam build",
    "(the camera keeps working without sound; nothing was recorded)"]


def helper_flags(probe=True):
    """The flags the installed helper advertises, from its own --help.

    `--help` is the only probe that is safe on an OLD helper: passing it an
    unknown option makes it exit(1), which would take `cam on` down with it -
    and a recording with it. Returns a set (empty when the helper is missing or
    did not answer)."""
    helper = helper_path()
    if helper is None:
        return set()
    try:
        st = helper.stat()
        key = (str(helper), int(st.st_mtime), int(st.st_size))
    except OSError:
        return set()
    hit = _CAPS_CACHE.get("v")
    if hit is not None and hit[0] == key:
        return hit[1]
    if not probe:
        return set()
    found = set()
    try:
        res = subprocess.run([str(helper), "--help"], capture_output=True,
                             timeout=CMD_TIMEOUT)
        text = (res.stdout + res.stderr).decode(errors="replace")
        found = set(re.findall(r"(--[a-z][a-z-]+)", text))
    except Exception:  # a hung --help is "no recording", not a hung command
        found = set()
    if len(_CAPS_CACHE) > 4:
        _CAPS_CACHE.clear()
    _CAPS_CACHE["v"] = (key, found)
    return found


def helper_flags_cached():
    """What `helper_flags()` last answered in this process, or None if nobody
    has asked. Diagnostics that must not spawn a helper (`doctor`) use this."""
    hit = _CAPS_CACHE.get("v")
    return hit[1] if hit else None


def recording_supported():
    """True when the installed helper looks record-aware from its own --help.
    Cached per helper build; spawns nothing but `--help`."""
    flags = helper_flags()
    return any(marker in flags for marker in RECORD_MARKERS)


def mic_supported():
    """True when the installed helper advertises the microphone flags at all.
    A helper too old for `--list-mics` is never handed `--mic`, `--list-mics`
    or `--check-mic-permission` - it would exit(1) on the unknown option."""
    flags = helper_flags()
    return any(marker in flags for marker in MIC_MARKERS)


def mic_permission():
    """(word, usable) for the microphone, read straight off the helper's
    `--check-mic-permission`: it prints exactly one of authorized|denied|
    restricted|notDetermined and exits 0/2/3/4.

    Read-only by design and never prompts (AGENTS.md: camera/microphone privacy
    is asked about by macOS once, on the user's own terms - a status command
    must not be the thing that produces a dialog)."""
    helper = helper_path()
    if helper is None or not mic_supported():
        return "unknown", False
    try:
        res = subprocess.run([str(helper), "--check-mic-permission"],
                             capture_output=True, timeout=30)
    except Exception:
        return "unknown", False
    text = (res.stdout + res.stderr).decode(errors="replace").strip().splitlines()
    word = MIC_PERMISSIONS.get(res.returncode)
    if word is None:
        # An old helper answers "I don't know the option" with code 1; say so
        # rather than invent a permission nobody asked about.
        return "unknown", False
    if text and text[0].strip() != word:
        return str(text[0].strip()), word == "authorized"
    return word, word == "authorized"


def mics():
    """(ok, lines): the microphones `cam record --mic NAME` can pick, listed by
    the helper itself (never by name-guessing from Python)."""
    helper = helper_path()
    if helper is None:
        return False, ["Camera helper missing - build it first:",
                       "  klipperformac cam build"]
    if not mic_supported():
        return False, NO_MIC
    try:
        res = subprocess.run([str(helper), "--list-mics"], capture_output=True,
                             timeout=30)
    except Exception as e:
        return False, ["Could not ask the helper for microphones: " + str(e)]
    out = [l.strip() for l in
           (res.stdout + res.stderr).decode(errors="replace").splitlines()
           if l.strip()]
    if res.returncode != 0:
        return False, out or ["Could not list microphones (code {}).".format(
            res.returncode)]
    return True, out or ["No microphone found on this Mac."]


def mic_permission_status():
    """(ok, lines) for `klipperformac cam mic-permission`. A denial is a real
    problem (exit code 1), 'notDetermined' is not: nothing has been asked yet
    and macOS will ask once, by itself, the first time a recording uses sound."""
    helper = helper_path()
    if helper is None:
        return False, ["Camera helper missing - build it first:",
                       "  klipperformac cam build"]
    if not mic_supported():
        return False, NO_MIC
    word, usable = mic_permission()
    if usable:
        return True, ["Microphone permission: authorized",
                      "Sound goes into recordings only, never into the "
                      "stream, and only when you ask for it with --mic."]
    if word == "notDetermined":
        return True, ["Microphone permission: notDetermined (nothing has been "
                      "asked yet)",
                      "macOS asks once, by itself, the first time you record "
                      "with --mic. This command never triggers that dialog."]
    if word == "unknown":
        return False, ["Microphone permission: unknown (the helper did not "
                       "answer).", "Try: klipperformac cam build"]
    return False, [
        "Microphone permission: {}.".format(word),
        "Allow it in System Settings > Privacy & Security > Microphone, then "
        "record again.",
        "Not offered there? Reset the microphone prompt with:",
        "  tccutil reset Microphone " + IDENTIFIER]


def prefs():
    p = dict(DEFAULT_PREFS)
    p.update({k: v for k, v in paths.cam_settings().items() if v is not None})
    return p


def save_prefs(**kwargs):
    return paths.save_cam_settings(**kwargs)


# The stored camera name is free text: a hand-edited settings.json can hold a
# value JSON reads back happily but the OS cannot encode (a lone surrogate),
# which would crash the spawn with a UnicodeEncodeError traceback.
BAD_DEVICE_NAME = ("The camera name in your settings has characters this Mac "
                   "cannot use, so it was not handed to the camera helper.")
BAD_DEVICE_FIX = ('Pick the camera again: klipperformac cam config --device '
                  '"name"   (list them: klipperformac cam list)')


def _device_argv(p):
    """["--device", NAME] when a camera name is stored, [] when it is not, and
    None when the stored name cannot be passed to another process at all."""
    device = p.get("device")
    if device is None:
        return []
    text = str(device)
    if not text:
        return []
    try:
        os.fsencode(text)          # what exec() does to every argv element
    except ValueError:             # UnicodeEncodeError: nothing we can spawn
        return None
    return ["--device", text]


def _as_bool(value):
    """settings.json is hand-editable: accept the words as well as the JSON
    booleans, and read anything unexpected as off (never default to recording)."""
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "yes", "1", "enabled")
    return bool(value)


def _record_status_reply(port, timeout=RECORD_TIMEOUT):
    """Ask the running helper how its own recording is doing:

        GET http://127.0.0.1:{port}/?action=recordstatus

    Read-only - the helper deliberately cannot be told to start or stop a
    recording over the network, only reported on (AGENTS.md 5: loopback only,
    nothing reaches another computer, and a process on this Mac cannot make the
    lens start capturing sound).

    Its body is key=value, one record per line:
        state=recording seconds=12.4 elapsed=12.5 bytes=4194304 audio=on
        file=/abs/path.mp4
        mic=authorized
    (or `state=idle record=not-requested mic=<word>` when this run was started
    without --record). Returns (code, dict) with code 0 when nobody answered.
    """
    query = "http://127.0.0.1:{}/?action=recordstatus".format(int(port))
    try:
        with urllib.request.urlopen(query, timeout=timeout) as reply:
            code, body = int(reply.getcode()), reply.read(4096)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048)
        except Exception:
            body = b""
        return int(e.code), {"text": (body or b"").decode(errors="replace")}
    except Exception as e:
        return 0, {"text": "the camera helper did not answer: {}".format(
            str(e)[:120])}
    out = {}
    for line in body.decode(errors="replace").replace("\r", "\n").split("\n"):
        key, sep, rest = line.strip().partition("=")
        if not sep or not key or " " in key:
            continue
        if key == "file":
            # `file=` is written alone on its line and a path may hold spaces,
            # so it is taken whole instead of being tokenised.
            out.setdefault("file", rest.strip())
            continue
        for name, _, value in [kv.partition("=") for kv in line.split(" ")]:
            if name and value and " " not in name:
                out.setdefault(name, value.strip())
    return code, out


def _record_number(info, key):
    try:
        return int(float(info.get(key)))
    except (TypeError, ValueError):
        return None


# Helper states that mean "a file is open right now". `done`/`failed` are kept
# separate on purpose: only these say the camera is busy recording.
RECORD_LIVE = ("starting", "recording", "stopping")


def _record_info(state=None):
    """The helper's own answer about its recording, normalised.

    One read-only GET (`?action=recordstatus`); see the contract in record().
    Returns a dict whose `head` is one of: recording, idle, done, failed,
    unsupported (a helper too old to have the endpoint), unreachable (nobody
    answered). Never raises, and never starts or stops anything - there is no
    such endpoint to ask.
    """
    state = state if state is not None else running()
    if not state:
        return {"head": "unreachable", "text": "the camera is off"}
    port = _valid_state_port(state.get("port")) or paths.CAM_PORT
    code, reply = _record_status_reply(port)
    if code == 404:
        return {"head": "unsupported"}
    if code == 0 or not reply or list(reply) == ["text"]:
        # Nobody answered, or what came back is not the key=value this helper
        # promises. Either way there is nothing to believe: no file, no length,
        # no state - and never a guess dressed up as the helper's answer.
        return {"head": "unreachable",
                "text": str(reply.get("text") or "")[:200]}
    head = str(reply.get("state") or "idle").strip().lower()
    out = {"head": "recording" if head in RECORD_LIVE else head,
           "state": head,
           "file": reply.get("file") or None,
           "bytes": _record_number(reply, "bytes"),
           "secs": _record_number(reply, "seconds"),
           "elapsed": _record_number(reply, "elapsed"),
           "audio": reply.get("audio") or "off",
           "mic": reply.get("mic") or ""}
    if reply.get("error"):
        out["error"] = str(reply["error"]).replace("_", " ")
    return out


def _valid_state_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def running():
    """Camera state dict (pid, started, port, width...) or None. A dead or
    borrowed pid unlinks the state file so the next start() is clean."""
    if not paths.CAM_STATE.exists():
        return None
    try:
        state = json.loads(paths.CAM_STATE.read_text())
    except (OSError, ValueError):
        return _clear_state()
    pid = state.get("pid") if isinstance(state, dict) else None
    bt = _process()._boot_time()
    if bt is None or not isinstance(pid, int) or state.get("boot_time") != bt:
        return _clear_state()
    if "port" in state and _valid_state_port(state.get("port")) is None:
        return _clear_state()
    cmd = _process()._live_cmds([pid]).get(str(pid), "")
    if CAM not in cmd or "--stream" not in cmd:
        return _clear_state()
    return state


def _clear_state():
    try:
        paths.CAM_STATE.unlink()
    except OSError:
        pass
    return None


def _save_state(pid, port, p):
    data = {"boot_time": _process()._boot_time(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pid": pid, "port": port}
    data.update({k: v for k, v in p.items()
                 if k in PREF_KEYS and v is not None})
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        paths.CAM_STATE.write_text(json.dumps(data, indent=2) + "\n")
        # 0600: this file names the camera, the port and any video path. It is
        # nobody's business, and it is what `cam off` trusts.
        os.chmod(str(paths.CAM_STATE), 0o600)
        return True
    except OSError:
        return False


def _update_state(**kwargs):
    """Merge keys into the live state file (recording bookkeeping). Returns
    None when there is no live state to write to - never resurrects a dead
    camera's file."""
    state = running()
    if state is None:
        return None
    for key, value in kwargs.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    try:
        paths.CAM_STATE.write_text(json.dumps(state, indent=2) + "\n")
        try:
            os.chmod(str(paths.CAM_STATE), 0o600)
        except OSError:
            pass
    except OSError:
        return None
    return state


def _scan_streams():
    """[(pid, cmdline)] for streaming helpers WE installed: the binary in
    paths.CAM_BIN or the copy inside an app bundle. Matching the two marker
    words alone would let `cam on` kill an unrelated process that merely
    mentions them, or another checkout's helper on its own port - so scope the
    scan to our executable, the way process.py scopes yours to .klipperformac.
    A helper run straight from KLIPPERFORMAC_CAM_BIN is tracked by the state
    file instead, which `cam off` uses."""
    try:
        res = subprocess.run(["ps", "-eo", "pid=,command="],
                             capture_output=True, timeout=CMD_TIMEOUT)
        out = res.stdout.decode(errors="replace")
    except Exception:  # a hung ps must not hang `cam on`
        return []
    own = (str(paths.CAM_BIN), "/Contents/Resources/" + CAM)
    hits = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        cmd = parts[1]
        if CAM in cmd and "--stream" in cmd and any(o in cmd for o in own):
            hits.append((pid, cmd))
    return hits


def _scan_pids():
    """Pids of our streaming helpers, tracked by us or not."""
    return [pid for pid, _ in _scan_streams()]


def _claims_port(cmd, port):
    """True when this helper wants our port. With no --port at all the helper
    falls back to its default, which is the port we are about to use."""
    want = str(port)
    for value in re.findall(r"--port[ \t=]+([0-9]+)", cmd):
        if value == want:
            return True
    return "--port" not in cmd


def _reap_orphans(port):
    """An untracked helper of ours still holding the port makes a fresh start
    fail. Only helpers that claim THIS port are signalled, so a camera
    streaming somewhere else keeps running untouched."""
    pids = [pid for pid, cmd in _scan_streams() if _claims_port(cmd, port)]
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    deadline = time.time() + 3.0
    live = list(pids)
    while live and time.time() < deadline:
        time.sleep(0.2)
        live = [p for p in live if _alive(p)]
    for pid in live:
        _signal(pid, signal.SIGKILL)
        _reap(pid)


def _log_tail(count=3):
    """Last few lines of the camera log (bounded read: it can grow big)."""
    try:
        with open(paths.CAM_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return [l[:200] for l in lines[-count:]]


def start(record=None, secs=None, mic=None):
    """Turn the camera on. Returns (ok, lines) for the user to read.

    `record` is the helper's own boot-time video mode: the same process that
    streams also writes one .mp4 (one AVCaptureSession per camera - the lens
    cannot be shared, so recording is never a second helper). `secs` lets the
    helper close the file and stop by itself; `mic` arms the microphone, which
    the helper only accepts together with `record`. With all three unset the
    argv is exactly what it has always been."""
    state = running()
    if state is not None:
        port = state.get("port") or paths.CAM_PORT
        return True, ["Camera already on: " + stream_url(port),
                      "Stop it with: klipperformac cam off"]
    helper = helper_path()
    if helper is None:
        return False, [
            "The camera helper (" + CAM + ") is not installed.",
            "Build it once: klipperformac cam build",
            "(the Klipper for Mac app also ships it inside its bundle)"]
    word, usable = permission()
    if word == "not determined":
        # Spawning is what triggers the one-time macOS prompt, so go ahead.
        prompt_line = ("macOS will ask once to allow camera access - click OK.")
    elif usable or word == "unknown":
        prompt_line = None
    else:
        # Not spawning: a stream that can only ever show a black frame
        # confuses more than this one clear instruction.
        return False, [
            "macOS has {} {} from the camera.".format(
                "blocked" if word == "denied" else "restricted", CAM),
            "Allow it in System Settings > Privacy & Security > Camera, "
            "then turn the camera on again.",
            "Not offered there? Reset the camera prompt with:",
            "  tccutil reset Camera " + IDENTIFIER]
    p = prefs()
    device = _device_argv(p)
    if device is None:
        # Checked before anything is reaped or spawned: a name we cannot pass
        # on is the user's to fix, not a reason to kill a working camera.
        return False, [BAD_DEVICE_NAME, BAD_DEVICE_FIX]
    port = paths.CAM_PORT
    _reap_orphans(port)
    if not _port_free(port):
        return False, [
            "Port {} is used by another app, so the camera has nowhere to "
            "stream.".format(port),
            "Quit that app, or pick another port: export "
            "KLIPPERFORMAC_CAM_PORT=8083"]
    args = [str(helper), "--stream", "--port", str(port)]
    for key in ("width", "height", "fps", "quality"):
        args += ["--" + key, str(p[key])]
    args += device
    # Recording flags go to the helper at boot, and only for a helper that
    # advertises them: an old one exits(1) on an unknown option, which would
    # take the camera down with it. --mic is only ever valid together with
    # --record (the helper says so itself), so sound is a recording choice.
    if record is not None:
        args += ["--record", str(record)]
        if secs:
            args += ["--seconds", str(int(secs))]
        # `--mic` bare means "whatever macOS picks", `--mic NAME` a chosen one.
        # An empty string is the bare flag, not "no microphone": only None (or
        # False) means sound was never asked for.
        if mic == "" or mic is True:
            args += ["--mic"]
        elif mic:
            args += ["--mic", str(mic)]
    paths.ensure_dirs()
    paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    # Where this run's own lines start in the shared log: the helper's stdout
    # ("Recording to ...", "Camera ready: ...", the close line) and its
    # "KlipperCam: ..." notes are the evidence for what a recording became, and
    # only the bytes written from here on belong to this run.
    log_at = _log_size()
    log = None
    try:
        log = open(paths.CAM_LOG, "ab")
        proc = _popen(args, stdout=log, stderr=log)
    except (OSError, ValueError) as e:
        # ValueError too: exec refuses an argv element with a NUL byte, and a
        # refused spawn is still just "could not start the camera".
        return False, ["Could not start the camera: " + str(e),
                       "Rebuild the helper if this keeps happening: "
                       "klipperformac cam build"]
    finally:
        if log is not None:
            log.close()
    _sleep(START_SETTLE)
    if not _alive(proc.pid):
        return False, ["The camera stopped as soon as it started."] + [
            "  " + l for l in _log_tail()] + [
            "Check details with: klipperformac cam status"]
    lines = []
    if prompt_line:
        lines.append(prompt_line)
    if not _save_state(proc.pid, port, p):
        lines.append("[!] Camera is on but not tracked; if 'cam off' does not "
                     "stop it, use: klipperformac killall")
    if record is not None:
        _update_state(record={"file": str(record), "secs": int(secs or 0),
                              "started": time.strftime(RECORD_TIME_FMT),
                              "audio": mic is not None, "log_at": log_at,
                              "mic": mic or ""})
    lines.append("Camera on: " + stream_url(port))
    lines.append("  Only this Mac can watch it (localhost).")
    lines.append("  Stop anytime: klipperformac cam off")
    lines.append("  The camera is OFF unless you turn it on - nothing streams "
                 "while it is off.")
    if record is not None:
        lines.append("  Recording: " + str(record) +
                     (" with sound" if mic is not None else " (picture only)"))
        if secs:
            lines.append("  It closes itself in {}s.".format(int(secs)))
        else:
            lines.append("  Stop it (and the camera) with: "
                         "klipperformac cam record off")
    lines += register_webcam()
    return True, lines


def stop(timeout=6.0, record_info=None):
    """Turn the camera off (TERM, then KILL stragglers). (ok, lines).

    `record_info` is a recording status this caller already read: the answer
    disappears with the process, so a command that read it before signalling
    must not have to knock twice."""
    state = running()
    if state is None:
        return False, ["Camera was not on."]
    pid = state.get("pid")
    # A recording this helper is writing is finalised by the very same SIGTERM
    # (the helper closes the file, then exits), so nothing extra is asked here -
    # but the user is told, because the file is what they care about.
    rec = _active_record(state)
    # Ask the helper what it has written BEFORE the signal: the answer is gone
    # the moment the process is, and it is the only number that knows the
    # recording's own length.
    if record_info is not None:
        info = record_info
    else:
        info = _record_info(state) if rec else {}
    extra = (["  Closing the recording first: {}".format(rec.get("file"))]
             if rec else [])
    if rec and timeout <= 6.0:
        # AVFoundation finishes a movie by closing the writer on a background
        # queue; the helper allows 20s of that before it deletes the file.
        timeout = RECORD_WAIT
    _signal(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        _sleep(0.2)
    if _alive(pid):
        _signal(pid, signal.SIGKILL)
    deadline = time.time() + 2.0
    while time.time() < deadline and _alive(pid):
        _reap(pid)
        _sleep(0.2)
    if _alive(pid):
        return False, ["[!] The camera (pid {}) refuses to die. Force it with: "
                       "klipperformac killall".format(pid)]
    _reap(pid)
    _clear_state()
    lines = ["Camera off - nothing is being captured."]
    lines += extra
    lines += _saved_lines(info, rec)
    lines += _webcam_disabled()
    lines.append("Turn it back on: klipperformac cam on")
    return True, lines


def _post_webcam(payload):
    """Best-effort live webcam update. Moonraker answers these endpoints over
    plain HTTP (TransportType.all), and loopback is a trusted client in our
    generated config, so no API key is needed. Silent on any failure."""
    url = "http://localhost:{}/server/webcams/item".format(paths.MOONRAKER_PORT)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read()
        return True
    except Exception:
        return False


def _webcam_payload():
    return {"name": configgen.WEBCAM_NAME, "enabled": True,
            "location": "printer", "service": "mjpegstreamer",
            "stream_url": stream_url(), "snapshot_url": snapshot_url()}


def register_webcam():
    """Make the stream visible to the web UI. Returns lines; never raises."""
    from . import process
    try:
        changed = configgen.ensure_webcam_section()
    except Exception as e:
        return ["[!] Could not write the camera section to moonraker.conf: " +
                str(e), "The stream itself still works: " + stream_url()]
    if not paths.MOONRAKER_CONF.exists():
        return ["No moonraker.conf yet, so the web UI cannot list the camera.",
                "Set up the stack first: klipperformac setup"]
    up = process.running() is not None
    if not (changed and up):
        return []  # nothing new for the user to do
    # Moonraker reads that section at startup. Its webcam endpoints also
    # answer plain HTTP on loopback (a trusted client in our config), so ask
    # it live first and only bother the user with a restart when that fails.
    if _post_webcam(_webcam_payload()):
        return ["The web UI shows the camera now (no restart needed)."]
    return ["restart the stack once to show the camera in the web UI "
            "(klipperformac restart)"]


def _uptime(started):
    try:
        secs = int(time.time() - time.mktime(
            time.strptime(started, "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a pre-1970 'started' has no time_t here.
        return "?"
    secs = max(secs, 0)
    if secs < 60:
        return "{}s".format(secs)
    if secs < 3600:
        return "{}m {}s".format(secs // 60, secs % 60)
    return "{}h {}m".format(secs // 3600, (secs % 3600) // 60)


def status():
    """Lines describing helper, permission, stream, prefs, config, port."""
    from . import process
    lines = []
    helper = helper_path()
    lines.append("  {:<12} {}".format(
        "helper", str(helper) if helper else
        "missing - build it: klipperformac cam build"))
    word, usable = permission()
    if helper is None:
        perm = "unknown (no helper)"
    elif usable:
        perm = word
    elif word == "not determined":
        perm = "not asked yet - macOS prompts the first time the camera starts"
    elif word == "unknown":
        perm = "unknown (the helper did not answer)"
    else:
        perm = "{} - System Settings > Privacy & Security > Camera".format(word)
    lines.append("  {:<12} {}".format("permission", perm))
    state = running()
    if state:
        lines.append("  {:<12} on (pid {}, up {})".format(
            "camera", state.get("pid"), _uptime(state.get("started"))))
        lines.append("  {:<12} {}".format("stream", stream_url(state.get("port"))))
    else:
        lines.append("  {:<12} off (nothing is being captured)".format("camera"))
        lines.append("  {:<12} {} - turn it on: klipperformac cam on".format(
            "stream", stream_url()))
    p = prefs()
    lines.append("  {:<12} {}x{} @ {}fps, quality {}, device {}".format(
        "prefs", p["width"], p["height"], p["fps"], p["quality"],
        p.get("device") or "automatic"))
    try:
        present, enabled = configgen.webcam_state()
        stack = process.running()
        if not present:
            moon = "no [webcam {}] section".format(configgen.WEBCAM_NAME)
        elif enabled:
            moon = "[webcam {}] on".format(configgen.WEBCAM_NAME)
            if not stack:
                moon += " (stack stopped; applies on klipperformac up)"
        else:
            moon = "[webcam {}] disabled".format(configgen.WEBCAM_NAME)
    except Exception:
        moon = "unknown (could not read moonraker.conf)"
    lines.append("  {:<12} {}".format("web UI", moon))
    if state is None and not _port_free(paths.CAM_PORT):
        lines.append("  {:<12} [!] port {} is taken by another app (move it: "
                     "export KLIPPERFORMAC_CAM_PORT=8083)".format(
                         "port", paths.CAM_PORT))
    # Only a helper that can record gets a line: the same keys and prose as
    # before for every install that cannot (the app reads these lines).
    try:
        rec = _active_record(state)
        if rec or (state and recording_supported()):
            if rec:
                lines.append("  {:<12} recording {} (started {})".format(
                    "video", rec.get("file"), rec.get("started") or "?"))
            else:
                lines.append("  {:<12} ready - klipperformac cam record <name>"
                             " [--seconds N] [--mic [NAME]]".format("video"))
    except Exception:
        pass  # a broken video line must never hide the camera status
    return lines


def snapshot(dest=None):
    """Grab one frame, from the running stream or directly from the helper.
    Returns (ok, lines) with the saved path in the first line."""
    if dest:
        dest = Path(dest).expanduser()
    else:
        dest = paths.TIMELAPSE / (time.strftime("%Y-%m-%d_%H%M%S") + ".jpg")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, ["Cannot save to {}: {}".format(dest.parent, e)]
    state = running()
    if state is not None:
        url = snapshot_url(state.get("port"))
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = r.read()
        except Exception as e:
            return False, ["The camera stream did not answer ({})".format(e),
                           "Look at: klipperformac cam status"]
        return _write_frame(dest, data)
    helper = helper_path()
    if helper is None:
        return False, ["Camera helper missing - build it first:",
                       "  klipperformac cam build"]
    # Same camera and size as the stream, so snapshots match what the web UI
    # shows. The helper gives up on its own after ~timeout+20s (it has to wait
    # for a permission click), so keep our own deadline longer than that.
    p = prefs()
    device = _device_argv(p)
    if device is None:
        return False, [BAD_DEVICE_NAME, BAD_DEVICE_FIX]
    args = [str(helper), "--snapshot", str(dest), "--timeout", "10",
            "--width", str(p["width"]), "--height", str(p["height"]),
            "--quality", str(p["quality"])]
    args += device
    try:
        res = subprocess.run(args, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, ["Taking a snapshot took too long (the camera may be "
                       "busy or asleep).",
                       "If macOS asked for permission, click OK and try again."]
    except (OSError, ValueError) as e:
        # ValueError too: exec refuses an argv element with a NUL byte.
        return False, ["Could not run the camera helper: " + str(e)]
    if res.returncode != 0 and not dest.exists():
        err = (res.stderr.decode(errors="replace").strip() or
               "the helper exited with code {}".format(res.returncode))
        return False, ["Snapshot failed: " + err[:300],
                       "If the camera is blocked: System Settings > Privacy "
                       "& Security > Camera"]
    if not dest.exists():
        return False, ["The helper reported success but wrote no file to " +
                       str(dest)]
    return True, ["Saved: " + str(dest)]


def _write_frame(dest, data):
    head = data[:16].lstrip().lower()
    if not data:
        return False, ["The camera sent an empty picture. Turn it off and on "
                       "again: klipperformac cam off && klipperformac cam on"]
    if head.startswith((b"<", b"{", b"[", b"error", b"not ")):
        return False, ["The camera sent a message instead of a picture: " +
                       data[:200].decode(errors="replace").strip(),
                       "See why: klipperformac cam status"]
    try:
        dest.write_bytes(data)
    except OSError as e:
        return False, ["Could not write {}: {}".format(dest, e)]
    return True, ["Saved: " + str(dest)]


def recordings_dir():
    """Where recorded video lives: DATA/recordings (late-bound, so `klipperformac
    data <dir>` relocation is honoured)."""
    return paths.DATA / RECORDINGS_NAME


def _env_mb(names, default):
    """A MB floor/cap from the environment, clamped to something sane; the
    default when unset or unreadable."""
    for name in names:
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            return default
        return max(1, min(val, 1024 * 1024)) * MB
    return default * MB


def _disk_target(path):
    """Nearest existing ancestor (statvfs needs a real path; the file we are
    about to create does not exist yet)."""
    cur = Path(str(path))
    for _ in range(12):
        try:
            if cur.exists():
                return cur
        except OSError:
            return Path(".")
        cur = cur.parent
    return Path(".")


def free_bytes(path=None):
    """Bytes free on the volume holding `path`. 0 when it cannot be measured:
    an unknown amount of disk is never a reason to start recording."""
    try:
        return int(shutil.disk_usage(str(_disk_target(path or recordings_dir())))
                   .free)
    except OSError:
        return 0


def _fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("kB", 1024)):
        if n >= div:
            return "{:.1f} {}".format(n / div, unit)
    return "{:.0f} B".format(n)


def _record_name(text, base):
    """The one file name a user-supplied destination may become, or a refusal.

    A bare name is placed in `base`; a path is honoured only while it really
    lands in `base` (a recording is found by looking at one folder, the same way
    every other user path in this CLI is guarded - nothing is silently rewritten
    and nothing is written outside the data folder)."""
    if text.startswith("~") or os.path.isabs(text):
        inside = os.path.realpath(os.path.expanduser(text))
        if os.path.dirname(inside) != os.path.realpath(str(base)):
            return None, ("Recordings go to {} - give a name there, or a path "
                          "inside it (not: {})".format(base, text))
    elif "/" in text or os.sep in text or (os.altsep and os.altsep in text):
        return None, ("Pick a plain file name (no folders), or a path inside "
                      "{}: {}".format(base, text))
    name = os.path.basename(text)
    if ".." in name or name.startswith("."):
        return None, ("Pick a file name that does not start with a dot and has "
                      "no '..' in it: " + name)
    if name.startswith("-"):
        return None, ("Pick a file name that does not start with '-': " + name)
    if len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None, ("That name is too long or has control characters in it "
                      "(keep it under 100 plain characters).")
    try:
        os.fsencode(name)
    except ValueError:
        return None, "That name has characters this Mac cannot store."
    if name.lower().endswith(RECORD_AUDIO_SUFFIX):
        return None, ("Sound alone is not recorded here: the helper writes one "
                      "video file with both tracks.")
    if not name.lower().endswith(RECORD_SUFFIX):
        name += RECORD_SUFFIX
    return name, None


def _record_target(value, stamp=None):
    """A recording file, always inside recordings_dir.

    Returns (path, None) or (None, why-not). On top of _record_name's guards the
    resolved target must still be the file we meant (a symlink parked there is
    refused, never followed out of the data folder), it must not be a folder,
    and it must not clobber a video that already has bytes in it."""
    base = recordings_dir()
    stamp = stamp or time.strftime(RECORD_TIME_FMT)
    text = "" if value is None else str(value).strip()
    if not text:
        name = stamp + RECORD_SUFFIX
    else:
        name, why = _record_name(text, base)
        if why:
            return None, why
    dest = Path(str(base)) / name
    base_real = os.path.realpath(str(base))
    real = os.path.realpath(str(dest))
    if os.path.dirname(real) != base_real or os.path.basename(real) != name:
        return None, ("{} would land outside the recordings folder (it is a "
                      "link to somewhere else).".format(dest))
    if os.path.isdir(str(dest)):
        return None, "{} is a folder, not a file.".format(dest)
    if os.path.exists(str(dest)) and os.path.getsize(str(dest)) > 0:
        return None, "{} already holds a video. Pick another name.".format(dest)
    return dest, None


def _record_files():
    """Our own recordings, oldest first (name pattern is the only thing prune()
    will accept), as [(path, size)]."""
    files = []
    try:
        entries = list(recordings_dir().iterdir())
    except OSError:
        return files
    for cand in entries:
        try:
            if not cand.is_file() or not RECORD_NAME_RE.match(cand.name):
                continue
            files.append((cand, cand.stat().st_size))
        except OSError:
            continue
    # Timestamped names sort chronologically; no second stat round-trip needed.
    files.sort(key=lambda pair: str(pair[0]))
    return files


def records_bytes():
    return sum(size for _, size in _record_files())


def prune_videos(limit=None, keep=None):
    """Delete our own oldest recordings until the folder fits under `limit`
    (default RECORD_CAP_MB). `keep` names are never touched (the file being
    written right now). Returns (freed_bytes, [deleted names]).

    Scope note: only files matching _record_files() are candidates, so a folder
    of hand-named videos the user put there is left completely alone."""
    limit = _env_mb(RECORD_ENVS, RECORD_CAP_MB) if limit is None else int(limit)
    keep = set(os.path.basename(str(k)) for k in (keep or []))
    files = _record_files()
    total = sum(size for _, size in files)
    gone = []
    freed = 0
    for path, size in files:
        if total <= limit:
            break
        if path.name in keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
        gone.append(path.name)
    return freed, gone


def _sleep(seconds):
    """One slice of a bounded wait (patched by the offline tests)."""
    time.sleep(seconds)


def _file_size(path):
    try:
        return Path(str(path)).stat().st_size
    except OSError:
        return None


def _saved_lines(info, rec):
    """What the video ended up being: the helper's own words plus the bytes
    really on disk. A recording that never got a picture is deleted by the
    helper itself on the way out, so this reports "no file" instead of a saved
    video - never a phantom length, and never a half-written file called a
    success."""
    since = (rec or {}).get("log_at")
    done = _recorded_report(since)
    path = ((done or {}).get("file") or info.get("file")
            or (rec or {}).get("file"))
    if not path:
        return []
    size = _file_size(path)
    if not size:
        return (["[!] No video was saved: {} is not there. The helper keeps no "
                 "half-written file, so this one either never got a picture or "
                 "did not close (exit code {}).".format(
                     path, info.get("exit"))]
                + _helper_notes(since))
    if not _finalised(info, done):
        # Bytes that no movie writer ever closed are not a video: they are a file
        # that plays back as garbage. Say which one this is.
        return (["[!] {} holds {} but was never closed: the camera died or was "
                 "killed mid-recording, so that is raw bytes, not a playable "
                 "video.".format(path, _fmt_size(size))] + _helper_notes(since))
    out = ["Saved: " + str(path), "  size {} on disk".format(_fmt_size(size))]
    if done:
        out.append("  length {:g}s, {} bytes, sound {} - as the camera helper "
                   "measured it".format(done["secs"], done["bytes"],
                                        done["audio"]))
        if size != done["bytes"]:
            out.append("  [!] the file is {} on disk although the helper "
                       "closed it at {} bytes".format(_fmt_size(size),
                                                      _fmt_size(done["bytes"])))
    elif info.get("secs") is not None:
        out.append("  length {}s".format(info["secs"]))
    if info.get("audio") and not done:
        out.append("  sound {}".format(info["audio"]))
    if info.get("error"):
        out.append("  [!] the helper reported: " + str(info["error"]))
    out += _helper_notes(since)
    return out


def _finalised(info, done):
    """Did the helper close this movie? Only its own words count: the `Recorded
    ...` line of THIS run (`done`, read from the byte offset it started at), a
    `state=done` it reported while it was up, or its exit 0.

    Without one of those, bytes on disk are a file with no finished atom - the
    very thing the helper's contract promises never to leave, and the one thing
    this module must never call a saved video."""
    if done:
        return True
    if str(info.get("state") or "") == "done":
        return True
    try:
        return int(info.get("exit")) == RECORD_OK
    except (TypeError, ValueError):
        return False


def _log_size():
    """Bytes already in the camera log - the offset a new run starts writing
    at, so its own lines can be told from an older run's."""
    try:
        return int(paths.CAM_LOG.stat().st_size)
    except (OSError, ValueError):
        return 0


def _helper_lines(since=None):
    """The helper's own stdout/stderr from the camera log, from byte `since`
    onwards. Bounded read: this log can grow big."""
    try:
        with open(paths.CAM_LOG, "rb") as handle:
            handle.seek(max(0, int(since or 0)))
            text = handle.read(262144).decode(errors="replace")
    except (OSError, ValueError):
        return []
    return [l.strip() for l in text.splitlines() if l.strip()]


def _recorded_report(since=None):
    """The last `Recorded <abs path> (<sec> s, <bytes> bytes, sound on|off)`
    line of this run, as a dict, or None when the helper never wrote one (it
    only ever writes that line for a file it finished and kept)."""
    out = None
    for line in _helper_lines(since):
        hit = RECORD_DONE_RE.match(line)
        if hit:
            out = {"file": hit.group(1), "secs": float(hit.group(2)),
                   "bytes": int(hit.group(3)), "audio": hit.group(4)}
    return out


def _helper_notes(since=None, limit=3):
    """The `KlipperCam: ...` sentences this run emitted, verbatim: refusals and
    the degraded paths ("No sound: ... Recording the picture only.", the
    unanswered microphone prompt, a close that had to be deleted). Quoted, not
    paraphrased - the helper knows why, this module does not."""
    notes = [hit.group(1) for hit in
             (HELPER_NOTE_RE.match(line) for line in _helper_lines(since))
             if hit]
    return ["  the helper said: " + note[:300] for note in notes[-limit:]]


def _wait_exit(pid, seconds):
    """Wait, bounded, for the helper we started to finish; its exit code, or
    None when it is still up at the deadline or has no code we can read.

    The waitpid is not decoration: our helper is a child of this command, and
    until it is reaped `ps` still lists it, so `_alive()` alone would wait
    forever and the exit code would be lost. 0 = a complete file, 3 = the helper
    deleted the file instead of leaving a broken one, 1 = it refused."""
    if not pid:
        return None
    deadline = time.time() + max(0.0, float(seconds))
    while True:
        try:
            gone, status = os.waitpid(int(pid), os.WNOHANG)
        except (ChildProcessError, OSError, ValueError):
            # Not our child (another command started it, or someone reaped it):
            # no exit code to report, so the file and the log are the evidence.
            return None
        if gone:
            _reap(int(pid))
            try:
                return os.waitstatus_to_exitcode(status)
            except (ValueError, AttributeError):
                return None
        if time.time() >= deadline:
            return None
        _sleep(RECORD_POLL)


def _busy_helper():
    """A streaming helper we cannot control (its state file is gone or was
    corrupted). The lens is held by THAT process: one AVCaptureSession per
    camera, so recording somewhere else would only fail confusingly."""
    pids = _scan_pids()
    return pids[0] if pids else None


def _active_record(state=None):
    """The recording this camera is writing, as a dict, or None. A hand-edited
    or half-written state file must never make `cam record` crash."""
    state = state if state is not None else running()
    if not state:
        return None
    rec = state.get("record")
    if not isinstance(rec, dict):
        return None
    path = rec.get("file")
    if not isinstance(path, str) or not path:
        return None
    try:
        os.fsencode(path)
    except ValueError:
        return None
    out = {"file": path}
    for key in ("started", "secs", "audio", "log_at"):
        value = rec.get(key)
        out[key] = value if isinstance(value, (int, float, str, bool)) else None
    return out


def _record_mic(mic, audio, p):
    """(value, why-not) for the microphone of THIS recording.

    None means "no sound" (nothing is handed to --mic). "" means the bare flag
    - the microphone macOS picks. Anything else is a name from `cam mics`.
    The command line wins over the stored setting, and an unusable name is a
    refusal rather than a silently soundless video: the user asked for sound."""
    if mic is not None:
        text = str(mic).strip()
        low = text.lower()
        if low in MIC_OFF_WORDS:
            return None, None
        if low in MIC_ON_WORDS:
            text = ""                 # what the helper itself reads --mic on as
    elif not _as_bool(p.get("audio")):
        return None, None
    else:
        text = str(p.get("mic") or "").strip()
    if audio is not None and not _as_bool(audio):
        return None, None
    try:
        os.fsencode(text)
    except ValueError:
        return None, ('The microphone name "{}" cannot be handed to the helper, '
                      "so nothing was recorded. Pick it again: klipperformac "
                      "cam config --mic \"name\" (list them: klipperformac "
                      "cam mics)".format(text[:40]))
    return text, None


def _record_secs(value):
    """'30' / '30s' / None -> (secs or None, why-not)."""
    if value is None:
        return None, None
    text = str(value).strip().lower()
    if not text:
        return None, None
    for suffix in ("secs", "sec", "s"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[:-len(suffix)]
            break
    try:
        secs = int(float(text))
    except (TypeError, ValueError):
        return None, ("'{}' is not a number of seconds. Use: klipperformac "
                      "cam record <name> --seconds 30".format(value))
    if not RECORD_MIN_SECS <= secs <= RECORD_MAX_SECS:
        return None, ("A timed recording must be {}-{} seconds ({} given); for "
                      "an open-ended one leave --seconds out and stop it with "
                      "`cam record off`.".format(RECORD_MIN_SECS,
                                                 RECORD_MAX_SECS, secs))
    return secs, None


def _webcam_disabled():
    """Take the managed [webcam] entry back out when the helper is gone; at most
    one advisory line, and never an exception from a bookkeeping detail."""
    from . import process
    try:
        if configgen.set_webcam_enabled(False) and process.running() is not None:
            return ["The web UI may still list the camera until the stack "
                    "restarts: klipperformac restart"]
    except Exception:
        pass
    return []


def _camera_retired():
    """The helper is gone by itself (a `--seconds` recording closes the file and
    stops the camera - that is the helper's contract, not a crash), so the state
    and the web UI entry go with it."""
    _clear_state()
    return _webcam_disabled()


def record(path=None, secs=None, mic=None, audio=None):
    """`klipperformac cam record <name> [--seconds N] [--mic [NAME]]`, plus
    `cam record off` and `cam record status`. Returns (ok, lines), cam.py style.
    Never touches klippy or Moonraker's files beyond the managed webcam entry.

    CONTRACT with the Swift helper (Sources/KlipperCam/main.swift,
    Recorder.swift, MjpegServer.swift) that shapes every line below - observed
    from it, never invented for it:
      flags    --record PATH (the live stream stays up too), --seconds N (the
               file is closed and the helper stops by itself; needs --record),
               --mic | --mic on|off|NAME (needs --record), --list-mics,
               --check-mic-permission (read-only, never prompts).
      stdout   "Recording to <abs path> (picture only|with sound)",
               "Camera ready: ...", "Microphone ready: <name>. Sound goes into
               the recording only, never into the stream.", and on close
               "Recorded <abs path> (<sec> s, <bytes> bytes, sound on|off)".
      stderr   "KlipperCam: ..." carries refusals and degraded paths, e.g.
               "No sound: <hint> Recording the picture only." and the
               unanswered-prompt sentence (30 s grace, picture only).
      exit     0 a complete file; 3 NO file left (never a corrupt mp4);
               1 bad usage or no camera permission.
      http     GET /?action=recordstatus -> key=value lines (state=recording
               seconds= elapsed= bytes= audio=on|off [error=], file=, mic=).
               READ ONLY on purpose: there is no start/stop endpoint, so nothing
               on this Mac - let alone another computer - can switch the lens on
               over the network (AGENTS.md 5).
    The lens belongs to ONE AVCaptureSession, so a recording is always made by
    the camera process itself: started with these flags when the camera is off,
    or that same process restarted on the same port when it is already
    streaming (the dashboard reconnects by itself).

    Capability is discovered, never assumed: `--help` is probed first
    (helper_flags) because an OLD helper exits 1 on an unknown option, which
    would take the camera down with the recording. An old helper is refused with
    update advice instead of being spawned with flags and pretended about.
    """
    text = "" if path is None else str(path).strip()
    low = text.lower()
    if low in ("off", "stop"):
        return record_stop()
    if low == "status":
        return record_status()
    helper = helper_path()
    if helper is None:
        return False, ["Camera helper missing - build it first:",
                       "  klipperformac cam build"]
    if not recording_supported():
        return False, NO_RECORD
    p = prefs()
    mic_value, why = _record_mic(mic, audio, p)
    if why:
        return False, [why]
    if mic_value is not None and not mic_supported():
        return False, NO_MIC
    secs, why = _record_secs(secs)
    if why:
        return False, [why]
    dest, why = _record_target(text or None)
    if why:
        return False, [why]
    state = running()
    rec = _active_record(state)
    if rec:
        return False, ["Already recording: " + str(rec.get("file")),
                       "Stop it with: klipperformac cam record off"]
    if state is None and _busy_helper():
        return False, [
            "The camera is held by a helper this command has no control of "
            "(pid {}, no state file).".format(_busy_helper()),
            "A recording is written by that same process, so nothing was "
            "started.",
            "Clear it and start fresh: klipperformac killall && "
            "klipperformac cam on"]
    need = _env_mb(FREE_ENVS, RECORD_MIN_FREE_MB)
    free = free_bytes(dest)
    if free < need:
        return False, [
            "Only {} free on {}, which is not enough to start a video "
            "(needs {}).".format(_fmt_size(free), dest.parent,
                                 _fmt_size(need)),
            "Old recordings of ours are pruned automatically; make room and "
            "try again: {}".format(recordings_dir())]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, ["Cannot create {}: {}".format(dest.parent, e)]
    was_on = state is not None
    if was_on:
        # The lens belongs to the running helper: restart that one process on
        # the same port with the recording flags on. Its own words are not
        # wanted here - the recording lines below are the answer.
        ok, stop_lines = stop()
        if not ok:
            return False, stop_lines + ["Nothing was recorded; the camera is "
                                        "exactly as it was."]
    ok, lines = start(record=dest, secs=secs, mic=mic_value)
    if not ok:
        back = start() if was_on else (False, [])
        return False, lines + (["  The camera is streaming again as before."]
                               if back[0] else
                               (["  The camera is down: " + str((back[1] or
                                                                 ["?"])[0])]
                                if was_on else
                                ["  The camera is off; nothing was recorded."]))
    started = _active_record(running() or {}) or {}
    since = started.get("log_at")
    out = ["Recording: " + str(dest)]
    if secs:
        out.append("  for {}s, then the file closes itself.".format(int(secs)))
    else:
        out.append("  until you stop it: klipperformac cam record off")
    if was_on:
        out.append("  The camera restarted on the same port for this - the "
                   "dashboard stream reconnects by itself.")
    out.append("  Sound: {}.".format(
        "on, microphone " + (mic_value or "the one macOS picks")
        if mic_value is not None else
        "off (the microphone is only ever used while recording, and the file "
        "stays on this Mac; ask for it with --mic)"))
    if not secs:
        return True, out
    # Timed: the helper closes the file and exits by itself. Watch it without
    # ever hanging: every wait here is bounded, and the file plus the helper's
    # own words are the evidence - never a claim invented here. The two extra
    # knocks are the settle time start() already spent before we got here.
    pid = (running() or {}).get("pid")
    for _ in range(int(round(float(secs) / RECORD_POLL)) + 2):
        if not _alive(pid):
            break
        _sleep(RECORD_POLL)
    code = _wait_exit(pid, RECORD_WAIT)
    if code is None and _alive(pid):
        # Overshot its own timer: the same stop button finalises the file.
        ok2, lines2 = record_stop()
        return ok2, ["(the recording did not close on its own)"] + lines2
    info = {"file": str(dest), "exit": code}
    done = _recorded_report(since)
    saved = _saved_lines(dict(info, log_at=since), {"file": str(dest),
                                                   "log_at": since})
    if code == RECORD_NO_FILE and not _file_size(dest):
        saved.insert(0, "Nothing to keep: the camera helper deleted the "
                        "unfinished file instead of leaving a broken one "
                        "(exit 3).")
    elif code not in (RECORD_OK, RECORD_NO_FILE, None):
        saved.insert(0, "[!] The camera stopped while recording (exit code {})."
                     .format(code))
    keep = [str(dest)] if _file_size(dest) else []
    freed, gone = prune_videos(keep=keep)
    if gone:
        saved.append("  freed {} by deleting {} older recording(s) of ours."
                     .format(_fmt_size(freed), len(gone)))
    saved += _camera_retired()
    return bool(keep) and _finalised(info, done), saved


def record_stop():
    """`klipperformac cam record off`: close the open video so the file is a
    real .mp4 (the `moov` atom is written on close).

    There is no stop command to send - the status endpoint is read-only and the
    helper finalises the movie on its way out, so this is the very same SIGTERM
    `cam off` gives it. Like `cam off`, it leaves the camera down: `klipperformac
    cam on` when you want pictures again.
    """
    state = running()
    if state is None:
        return False, ["The camera is off, so nothing was recording.",
                       "Start one: klipperformac cam record <name> "
                       "[--seconds N]"]
    rec = _active_record(state)
    info = _record_info(state)
    if info.get("head") == "unsupported":
        return False, NO_RECORD + ["Nothing was being recorded, so no video was "
                                   "lost."]
    if info.get("head") != "recording" and not rec:
        return False, ["Nothing was recording.",
                       "Start one: klipperformac cam record <name> "
                       "[--seconds N]"]
    if info.get("head") == "unreachable":
        # Up but not answering: say so rather than claim a file.
        return False, ["The camera helper did not answer, so nothing is known "
                       "about its recording."] + (
            ["The file may still be open: {}".format(rec.get("file"))]
            if rec else [])
    ok, lines = stop(record_info=info)
    if not ok:
        return False, lines + ["The recording may still be open: {}".format(
            (rec or {}).get("file") or info.get("file"))]
    path = info.get("file") or (rec or {}).get("file")
    done = _recorded_report((rec or {}).get("log_at"))
    keep = [str(path)] if path and _file_size(path) else []
    freed, gone = prune_videos(keep=keep)
    if gone:
        lines.append("  freed {} by deleting {} older recording(s) of ours."
                     .format(_fmt_size(freed), len(gone)))
    if not path:
        lines.append("[!] The state file named no recording, so the video can "
                     "only be found by looking in " + str(recordings_dir()))
    return bool(keep) and _finalised(info, done), lines


def record_status():
    """`klipperformac cam record status`: what the helper says, plus what is on
    disk here. Read-only, spawns nothing."""
    lines = []
    state = running()
    helper = helper_path()
    if helper is None:
        return True, [
            "  {:<12} no helper (build it: klipperformac cam build)".format(
                "recording")]
    if not recording_supported():
        return True, [
            "  {:<12} not supported by the installed helper (update it: "
            "klipperformac cam build)".format("recording"),
            "  {:<12} {}".format("videos", str(recordings_dir()))]
    if state is None:
        pid = _busy_helper()
        return True, [
            "  {:<12} idle (the camera is off{})".format(
                "recording", "; an untracked helper holds it, pid {}".format(
                    pid) if pid else ""),
            "  {:<12} klipperformac cam record <name> [--seconds N]".format(
                "start")]
    info = _record_info(state)
    rec = _active_record(state)
    if info.get("head") == "unsupported":
        lines.append("  {:<12} the installed helper has no recording status "
                     "- update it: klipperformac cam build".format("recording"))
    elif info.get("head") == "unreachable":
        lines.append("  {:<12} the running helper did not answer ({})".format(
            "recording", str(info.get("text", ""))[:80]))
    elif info.get("head") == "recording":
        lines.append("  {:<12} capturing {}".format(
            "recording", info.get("file") or (rec or {}).get("file") or "?"))
        if info.get("bytes") is not None:
            lines.append("  {:<12} {}".format("so far",
                                             _fmt_size(info["bytes"])))
        if info.get("secs") is not None:
            lines.append("  {:<12} {}s".format("length", info["secs"]))
        lines.append("  {:<12} {}".format("sound", info.get("audio") or "off"))
        if info.get("mic"):
            lines.append("  {:<12} {}".format("mic perm", info["mic"]))
        if info.get("error"):
            lines.append("  {:<12} [!] {}".format("problem", info["error"]))
        if rec and rec.get("secs"):
            lines.append("  {:<12} stops after {}s".format("timer",
                                                           rec["secs"]))
    elif info.get("head") in ("done", "failed"):
        # The movie is closed (or was given up on) while the helper is still up:
        # say which, and where the file is - never "recording", which it is not.
        lines.append("  {:<12} {} ({})".format(
            "recording", "closed" if info["head"] == "done" else "FAILED",
            info.get("file") or "?"))
        if info.get("bytes"):
            lines.append("  {:<12} {}".format("so far",
                                             _fmt_size(info["bytes"])))
        if info.get("error"):
            lines.append("  {:<12} [!] {}".format("problem", info["error"]))
    else:
        lines.append("  {:<12} idle (this camera run was started without a "
                     "recording)".format("recording"))
        lines.append("  {:<12} klipperformac cam record <name> "
                     "[--seconds N] [--mic [NAME]]".format("start"))
    files = _record_files()
    total = sum(size for _, size in files)
    lines.append("  {:<12} {} file(s), {} of {}".format(
        "videos", len(files), _fmt_size(total), _fmt_size(
            _env_mb(RECORD_ENVS, RECORD_CAP_MB))))
    if files:
        lines.append("  {:<12} newest {}".format("", files[-1][0].name))
    lines.append("  {:<12} {} free on {}".format(
        "disk", _fmt_size(free_bytes(recordings_dir())),
        recordings_dir()))
    p = prefs()
    lines.append("  {:<12} {}".format(
        "sound", "on by default (microphone {})".format(
            p.get("mic") or "the one macOS picks") if _as_bool(p.get("audio"))
        else "off unless you ask for --mic"))
    return True, lines


def list_devices():
    """(ok, lines) from the helper's camera list (Continuity Camera included)."""
    helper = helper_path()
    if helper is None:
        return False, ["Camera helper missing - build it first:",
                       "  klipperformac cam build"]
    try:
        res = subprocess.run([str(helper), "--list"], capture_output=True,
                             timeout=30)
    except Exception as e:
        return False, ["Could not ask the helper for cameras: " + str(e)]
    out = [l.strip() for l in
           (res.stdout + res.stderr).decode(errors="replace").splitlines()
           if l.strip()]
    if res.returncode != 0:
        return False, out or ["Could not list cameras (code {}).".format(
            res.returncode)]
    return True, out or ["No cameras found."]


def kill_orphans():
    """SIGKILL every streaming helper, tracked or not (used by killall).
    Returns the killed (name, pid) list."""
    pids = _scan_pids()
    for pid in pids:
        _signal(pid, signal.SIGKILL)
    deadline = time.time() + 3.0
    live = list(pids)
    while live and time.time() < deadline:
        time.sleep(0.2)
        gone = [p for p in live if not _alive(p)]
        for pid in gone:
            _reap(pid)
        live = [p for p in live if p not in gone]
    if not live:
        _clear_state()
    return [(CAM, pid) for pid in pids]


def _check_helper():
    helper = helper_path()
    if helper is None:
        return False, ("camera helper missing - build it once: "
                       "klipperformac cam build")
    return True, "camera helper: {}".format(helper)


def _check_permission():
    helper = helper_path()
    if helper is None:
        return True, "camera permission: not checkable without the helper"
    word, usable = permission()
    if usable:
        return True, "camera permission: " + word
    if word == "not determined":
        return True, ("camera permission: not asked yet - macOS prompts once, "
                      "click OK")
    if word == "unknown":
        return False, "camera permission: unknown (the helper did not answer)"
    return False, ("camera permission: {} - allow it in System Settings > "
                   "Privacy & Security > Camera".format(word))


def _check_port():
    state = running()
    if state is not None:
        port = _valid_state_port(state.get("port")) or paths.CAM_PORT
        return True, "camera port {}: streaming".format(
            port)
    if _port_free(paths.CAM_PORT):
        return True, "camera port {}: free".format(paths.CAM_PORT)
    return False, ("camera port {}: taken by another app (or an untracked "
                   "camera - clear it with: klipperformac killall)").format(
                       paths.CAM_PORT)


def _check_state():
    had = paths.CAM_STATE.exists()
    state = running()
    if state is not None:
        return True, "camera running (pid {})".format(state.get("pid"))
    if had:
        return True, "camera off (stale state from a crash was cleaned up)"
    return True, "camera off"


def _check_prefs():
    p = prefs()
    bad = []
    for key, lo, hi in (("fps", 1, 60), ("quality", 1, 100),
                        ("width", 160, 4096), ("height", 120, 4096)):
        try:
            val = int(p.get(key))
        except (TypeError, ValueError, OverflowError):
            # OverflowError too: int(float('inf')) is not a number of pixels.
            bad.append("{}={!r} (needs a number)".format(key, p.get(key)))
            continue
        if not lo <= val <= hi:
            bad.append("{}={} (use {}-{})".format(key, val, lo, hi))
    if bad:
        return False, "camera preferences look wrong: " + ", ".join(bad)
    return True, "camera preferences: {}x{} @ {}fps, quality {}".format(
        p["width"], p["height"], p["fps"], p["quality"])


def _check_log():
    try:
        size = paths.CAM_LOG.stat().st_size
    except OSError:
        return True, "camera log: none yet"
    if size > LOG_WARN_BYTES:
        return False, "camera log is {:.0f} MB - safe to delete: {}".format(
            size / 1048576.0, paths.CAM_LOG)
    return True, "camera log: {:.1f} MB".format(size / 1048576.0)


def _check_recording():
    """Recording capability for `doctor`. Two rules shape it: it must not spawn
    a helper (the camera check above already asked the helper once, and doctor
    stays honest about that), and it must not lie - so a stopped camera reports
    what is certain (folders, limits, free space) and defers the helper's own
    answer to `cam record status`."""
    state = running()
    cap = _env_mb(RECORD_ENVS, RECORD_CAP_MB)
    free = free_bytes(recordings_dir())
    floor = _env_mb(FREE_ENVS, RECORD_MIN_FREE_MB)
    used = records_bytes()
    room = "cap {}, {} free, {} of videos in {}".format(
        _fmt_size(cap), _fmt_size(free), _fmt_size(used), recordings_dir())
    if free < floor:
        return False, ("recording: only {} free on {} ({} needed to start a "
                       "video; {} of ours are pruned automatically)".format(
                           _fmt_size(free), recordings_dir(), _fmt_size(floor),
                           RECORDINGS_NAME))
    if used > cap:
        return False, ("recording: {} of videos over the {} cap - the next "
                       "recording to stop prunes the oldest of ours"
                       .format(_fmt_size(used), _fmt_size(cap)))
    if state is None:
        flags = helper_flags_cached()
        if flags is not None and not recording_supported():
            return True, ("recording: not in this helper build (it streams and "
                          "snapshots only; `klipperformac cam build` adds "
                          "video); " + room)
        return True, ("recording: video goes to {} - {} ; the helper's own "
                      "answer comes with `cam record status` once the camera "
                      "is on".format(recordings_dir(), room))
    info = _record_info(state)
    head = info.get("head")
    if head == "unsupported":
        return True, ("recording: not in this helper build - rebuild it: "
                      "klipperformac cam build; " + room)
    if head == "unreachable":
        return True, "recording: the streaming helper did not answer; " + room
    if head == "recording":
        return True, ("recording: capturing {} ({}); {}".format(
            info.get("file") or "?", _fmt_size(info.get("bytes") or 0), room))
    return True, "recording: ready; " + room


def doctor():
    """[(ok, msg)] camera checks for `klipperformac doctor`. Never raises.

    The recording capability is only reported when there is a helper to report
    on: a bare install already says "camera helper missing" one line above, and
    `doctor` must stay as quiet as it was before video existed."""
    checks = [_check_helper, _check_permission, _check_port, _check_state,
              _check_prefs, _check_log]
    try:
        if helper_path() is not None:
            checks.append(_check_recording)
    except Exception:
        pass
    out = []
    for check in checks:
        try:
            out.append(check())
        except Exception as e:  # a broken camera check must not stop doctor
            out.append((False, "camera check '{}' failed: {}".format(
                check.__name__, e)))
    return out
