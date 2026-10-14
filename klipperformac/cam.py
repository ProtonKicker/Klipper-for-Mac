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
import urllib.request
from pathlib import Path

from . import configgen, paths

CAM = "KlipperCam"
DEFAULT_PREFS = {"width": 1280, "height": 720, "fps": 15, "quality": 80}
PREF_KEYS = ("width", "height", "fps", "quality", "device")
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
            if path.is_file():
                return path
    if paths.CAM_BIN.is_file():
        return paths.CAM_BIN
    for cand in GUI_BUNDLES:
        inside = Path(cand).expanduser()
        if not inside.is_file():
            continue
        try:
            paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
            if _needs_copy(inside, paths.CAM_BIN):
                shutil.copy2(str(inside), str(paths.CAM_BIN))
                os.chmod(str(paths.CAM_BIN), 0o755)
        except OSError:
            return inside  # read-only bundle etc.: use it where it is
        return paths.CAM_BIN
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


def prefs():
    p = dict(DEFAULT_PREFS)
    p.update({k: v for k, v in paths.cam_settings().items() if v is not None})
    return p


def save_prefs(**kwargs):
    return paths.save_cam_settings(**kwargs)


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
        return True
    except OSError:
        return False


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


def start():
    """Turn the camera on. Returns (ok, lines) for the user to read."""
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
    port = paths.CAM_PORT
    _reap_orphans(port)
    if not _port_free(port):
        return False, [
            "Port {} is used by another app, so the camera has nowhere to "
            "stream.".format(port),
            "Quit that app, or pick another port: export "
            "KLIPPERFORMAC_CAM_PORT=8083"]
    p = prefs()
    args = [str(helper), "--stream", "--port", str(port)]
    for key in ("width", "height", "fps", "quality"):
        args += ["--" + key, str(p[key])]
    if p.get("device"):
        args += ["--device", str(p["device"])]
    paths.ensure_dirs()
    paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = None
    try:
        log = open(paths.CAM_LOG, "ab")
        proc = _popen(args, stdout=log, stderr=log)
    except OSError as e:
        return False, ["Could not start the camera: " + str(e),
                       "Rebuild the helper if this keeps happening: "
                       "klipperformac cam build"]
    finally:
        if log is not None:
            log.close()
    time.sleep(0.6)
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
    lines.append("Camera on: " + stream_url(port))
    lines.append("  Only this Mac can watch it (localhost).")
    lines.append("  Stop anytime: klipperformac cam off")
    lines.append("  The camera is OFF unless you turn it on - nothing streams "
                 "while it is off.")
    lines += register_webcam()
    return True, lines


def stop(timeout=6.0):
    """Turn the camera off (TERM, then KILL stragglers). (ok, lines)."""
    from . import process
    state = running()
    if state is None:
        return False, ["Camera was not on."]
    pid = state.get("pid")
    _signal(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    if _alive(pid):
        _signal(pid, signal.SIGKILL)
    deadline = time.time() + 2.0
    while time.time() < deadline and _alive(pid):
        _reap(pid)
        time.sleep(0.2)
    if _alive(pid):
        return False, ["[!] The camera (pid {}) refuses to die. Force it with: "
                       "klipperformac killall".format(pid)]
    _reap(pid)
    _clear_state()
    lines = ["Camera off - nothing is being captured."]
    try:
        if configgen.set_webcam_enabled(False) and \
                process.running() is not None:
            lines.append("The web UI may still list the camera until the stack "
                         "restarts: klipperformac restart")
    except Exception:
        pass
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
    except (TypeError, ValueError):
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
    args = [str(helper), "--snapshot", str(dest), "--timeout", "10",
            "--width", str(p["width"]), "--height", str(p["height"]),
            "--quality", str(p["quality"])]
    if p.get("device"):
        args += ["--device", str(p["device"])]
    try:
        res = subprocess.run(args, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, ["Taking a snapshot took too long (the camera may be "
                       "busy or asleep).",
                       "If macOS asked for permission, click OK and try again."]
    except OSError as e:
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
        return True, "camera port {}: streaming".format(
            state.get("port") or paths.CAM_PORT)
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
        except (TypeError, ValueError):
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


def doctor():
    """[(ok, msg)] camera checks for `klipperformac doctor`. Never raises."""
    out = []
    for check in (_check_helper, _check_permission, _check_port, _check_state,
                  _check_prefs, _check_log):
        try:
            out.append(check())
        except Exception as e:  # a broken camera check must not stop doctor
            out.append((False, "camera check '{}' failed: {}".format(
                check.__name__, e)))
    return out
