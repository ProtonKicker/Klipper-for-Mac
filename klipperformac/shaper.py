"""Zero-friction Python 3.14 for SHAPER_CALIBRATE.

Klipper imports numpy IN-PROCESS (klippy/extras/shaper_calibrate.py does an
importlib.import_module('numpy')) while upstream's klippy-requirements.txt
deliberately leaves numpy out, so the main venv can never run SHAPER_CALIBRATE.
Instead of editing upstream or adding numpy next to Moonraker's pinned deps, we
build a second venv (paths.SHAPER_VENV) from Python 3.14 with the upstream
requirements plus numpy, and use it as klippy's interpreter only when
printer.cfg asks for shaper work. Moonraker always stays on paths.VENV_PY and
nothing upstream is modified.
"""
import glob
import os
import re
import shutil
import subprocess

from . import paths
from .compat import build_env

PY_VERSION = "3.14"
PY_ENV = "KLIPPERFORMAC_SHAPER_PY"
BREW_PY = "python@3.14"
BREW_PATHS = ("/usr/local/bin/brew", "/opt/homebrew/bin/brew")
FALLBACK_PYTHONS = ("/usr/local/opt/python@3.14/bin/python3.14",
                    "/opt/homebrew/opt/python@3.14/bin/python3.14",
                    "/Library/Frameworks/Python.framework/Versions/3.14/bin"
                    "/python3.14")
CELLAR_GLOBS = ("/usr/local/Cellar/python@3.14/*/bin/python3.14",
                "/opt/homebrew/Cellar/python@3.14/*/bin/python3.14")
# Klipper's own requirement candidates (same order installer.py uses).
REQ_FILES = ("scripts/klippy-requirements.txt", "klippy/requirements.txt")
# The same package set as klippy-requirements.txt plus numpy, version-free:
# the pinned upstream set can lag a brand new interpreter, so it is the retry.
UNPINNED = ("numpy", "greenlet", "cffi", "jinja2", "markupsafe", "pyserial",
            "python-can", "setuptools", "msgspec")

PROBE_TIMEOUT = 20
SMOKE_TIMEOUT = 60
BUILD_TIMEOUT = 180
VENV_TIMEOUT = 300
PIP_TIMEOUT = 600
BREW_TIMEOUT = 600

SECTION_RE = re.compile(r"(?i)^\[\s*(input_shaper|resonance_tester)\s*\]")
COMMAND_RE = re.compile(r"(?i)\bshaper_calibrate\b")

SHAPER_READY = "klippy runs on python 3.14 shaper venv"


def _run(cmd, timeout, **kw):
    """argv list, captured output, hard timeout: (code, out, err), never raises."""
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    except Exception as e:
        return None, "", str(e)
    return (res.returncode, res.stdout.decode(errors="replace"),
            res.stderr.decode(errors="replace"))


def _brief(text, limit=300):
    """One line of subprocess output; pip/gcc logs are long and unhelpful raw."""
    clean = " ".join((text or "").split())
    return clean[-limit:] if clean else "(no output)"


def needs_shaper(cfg_path=None):
    """True when the config activates input shaping or resonance testing.

    Comment lines never count: Klipper's parser treats both '#' and ';' as
    full-line comment prefixes, so a documented-out '[input_shaper]' stays off.
    """
    path = cfg_path or paths.PRINTER_CFG
    try:
        with open(os.fspath(path), errors="replace") as f:
            text = f.read()
    except (OSError, ValueError):
        return False
    for line in text.splitlines():
        active = line.strip()
        if not active or active[0] in "#;":
            continue
        if SECTION_RE.match(active) or COMMAND_RE.search(active):
            return True
    return False


def _version_of(candidate):
    code, out, _ = _run([candidate, "-c",
                         "import sys; print('%d.%d' % sys.version_info[:2])"],
                        PROBE_TIMEOUT)
    return out.strip() if code == 0 else ""


def find_python314():
    """First interpreter present that really reports 3.14, or None.

    Homebrew moves python@3.14 between Cellar versions on every upgrade and
    symlinks it into /usr/local (Intel) or /opt/homebrew (Apple Silicon), so
    every known layout is probed instead of a single hardcoded path.
    """
    env = os.environ.get(PY_ENV)
    candidates = [env] if env else []
    candidates.append(shutil.which("python3.14") or "")
    candidates.extend(FALLBACK_PYTHONS)
    for pattern in CELLAR_GLOBS:
        candidates.extend(sorted(glob.glob(pattern), reverse=True))
    seen = set()
    for cand in candidates:
        if not cand or cand in seen or not os.path.exists(cand):
            continue
        seen.add(cand)
        if _version_of(cand) == PY_VERSION:
            return cand
    return None


def brew_available():
    """Homebrew is /usr/local on Intel and /opt/homebrew on Apple Silicon."""
    return bool(shutil.which("brew")) or any(os.path.exists(p)
                                             for p in BREW_PATHS)


def _requirements_file():
    for rel in REQ_FILES:
        req = paths.KLIPPER / rel
        if req.exists():
            return req
    return None


def _pinned_specs(req):
    """{package: spec} of a requirements file, markers and comments dropped."""
    specs = {}
    try:
        with open(req, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return specs
    for line in lines:
        spec = line.split("#", 1)[0].strip()
        if spec:
            specs[re.split(r"[=<>!~;\[\s]", spec, maxsplit=1)[0].lower()] = spec
    return specs


def _chelper_smoke(py):
    """Build+load c_helper.so exactly like klippy does at startup: upstream's
    own mtime check decides whether gcc runs, and nothing here edits the
    checkout (the .so is git-excluded by installer.py)."""
    script = (
        "import sys; sys.path.insert(0, {klippy!r}); "
        "import chelper; chelper.get_ffi(); print('c_helper built and loaded')"
    ).format(klippy=str(paths.KLIPPER / "klippy"))
    return _run([py, "-c", script], BUILD_TIMEOUT, env=build_env())


def _probe():
    """(ok, detail) for the two cheap gates healthy() stands on."""
    py = str(paths.SHAPER_PY)
    if not paths.SHAPER_PY.exists():
        return False, "shaper venv missing ({})".format(paths.SHAPER_VENV)
    code, _, err = _run([py, "-c", "import numpy"], PROBE_TIMEOUT)
    if code != 0:
        return False, "numpy is not importable in the shaper venv: " + _brief(err)
    code, _, err = _chelper_smoke(py)
    if code != 0:
        return False, "c_helper does not build with the shaper venv: " + _brief(err)
    return True, SHAPER_READY


def healthy(quiet=True):
    """True when klippy can actually run SHAPER_CALIBRATE on this venv.

    Two subprocesses (numpy import, c_helper load), no network; the c_helper
    compile only happens once, since upstream Klipper skips it while the .so
    is newer than its sources. Safe to call from status/doctor any time.
    """
    ok, detail = _probe()
    if not ok and not quiet:
        print("[!] shaper venv not ready: " + detail)
    return ok


def create_shaper_venv(interpreter=None, verbose=True, force=False):
    """(ok, messages): create/repair paths.SHAPER_VENV and prove it works.

    Only ever touches the shaper venv; the main venv is never deleted or
    rebuilt, and upstream files are only read.
    """
    messages = []

    def _say(msg):
        messages.append(msg)
        if verbose:
            print(msg)

    if paths.SHAPER_VENV.exists() and healthy() and not force:
        _say("Shaper venv already ready: {}".format(paths.SHAPER_VENV))
        return True, messages
    if interpreter is None:
        interpreter = find_python314()
    if not interpreter:
        _say("[!] No Python {} found; install it with 'brew install {}'"
             .format(PY_VERSION, BREW_PY))
        return False, messages
    cmd = [interpreter, "-m", "venv"]
    if force and paths.SHAPER_VENV.exists():
        cmd.append("--clear")  # a stale/partial venv is ours, safe to wipe
    cmd.append(str(paths.SHAPER_VENV))
    _say("Creating the shaper venv with {}...".format(interpreter))
    code, _, err = _run(cmd, VENV_TIMEOUT)
    if code != 0:
        _say("[!] venv creation failed: " + _brief(err))
        return False, messages
    py = str(paths.SHAPER_PY)
    req = _requirements_file()
    pinned = _pinned_specs(req) if req else {}
    code = None
    if req is None:
        _say("[!] No klippy requirements file under {}; installing the "
             "package set unpinned".format(paths.KLIPPER))
    else:
        _say("Installing {} as-is...".format(req))
        code, _, err = _run([py, "-m", "pip", "install", "--quiet",
                             "--upgrade", "-r", str(req)], PIP_TIMEOUT)
        if code == 0:
            _say("Upstream klippy requirements installed as-is (pristine)")
    if code != 0:
        if code is not None:
            _say("[!] pip rejected the pinned upstream requirements: "
                 + _brief(err))
        _say("Retrying the same package set unpinned:")
        for name in UNPINNED:
            spec = pinned.get(name.lower())
            _say("  {} -> unpinned{}".format(
                name, " (was {})".format(spec) if spec else ""))
        code, _, err = _run([py, "-m", "pip", "install", "--quiet",
                             "--upgrade"] + list(UNPINNED), PIP_TIMEOUT)
        if code != 0:
            _say("[!] pip install failed: " + _brief(err))
            return False, messages
    code, _, err = _run([py, "-c", "import numpy"], PROBE_TIMEOUT)
    if code != 0:
        _say("numpy is not part of klippy's requirements; installing it...")
        code, _, err = _run([py, "-m", "pip", "install", "--quiet",
                             "--upgrade", "numpy"], PIP_TIMEOUT)
        if code != 0:
            _say("[!] numpy install failed: " + _brief(err))
            return False, messages
    ok, detail = _probe()
    _say("Shaper venv ready: {}".format(detail) if ok else
         "[!] Shaper venv NOT ready: " + detail)
    return ok, messages


def interpreter_for_klipper():
    """(python path, note) for klippy; moonraker must keep using VENV_PY.

    Never raises: a broken shaper venv must degrade to 'no shaper today', not
    take the whole stack down with it.
    """
    try:
        if not needs_shaper():
            return str(paths.VENV_PY), None
        if healthy():
            return str(paths.SHAPER_PY), SHAPER_READY
        return (str(paths.VENV_PY),
                "[!] printer.cfg wants SHAPER_CALIBRATE, but the Python 3.14 "
                "shaper venv is not ready: klippy stays on the main venv and "
                "SHAPER_CALIBRATE will fail until the shaper venv is built "
                "(run: klipperformac setup)")
    except Exception:
        return str(paths.VENV_PY), None


def readiness():
    """State snapshot for doctor/status; no venv is created or modified."""
    needed = found = None
    venv_exists = ok = False
    detail = ""
    try:
        needed = needs_shaper()
        found = find_python314()
        venv_exists = paths.SHAPER_PY.exists()
        ok = _probe()[0] if venv_exists else False
        detail = _detail(needed, found, venv_exists, ok)
    except Exception as e:
        detail = "shaper status could not be determined: {}".format(e)
    return {"needed": bool(needed), "python_found": found,
            "venv_exists": venv_exists, "healthy": ok, "detail": detail}


def _detail(needed, python_found, venv_exists, ok):
    if ok:
        return ("Shaper venv ready: klippy can run SHAPER_CALIBRATE on Python {}"
                .format(PY_VERSION))
    if not venv_exists:
        if not needed:
            return ("No shaper venv yet (printer.cfg does not ask for "
                    "SHAPER_CALIBRATE)")
        if not python_found:
            return ("printer.cfg needs SHAPER_CALIBRATE: install Python {} "
                    "(brew install {}) and run setup".format(
                        PY_VERSION, BREW_PY))
        return ("printer.cfg needs SHAPER_CALIBRATE: run setup to build the "
                "Python {} shaper venv".format(PY_VERSION))
    return ("The shaper venv exists but is not working: re-run setup to "
            "repair it")


def ensure_shaper(with_shaper=False, install_python=False, verbose=True):
    """(ready, lines) for setup. Reports only: never raises, never SystemExit,
    never aborts setup and never touches the main venv or KLIPPERFORMAC_PYTHON.
    """
    lines = []

    def _say(msg):
        lines.append(msg)
        if verbose:
            print(msg)

    try:
        if not (with_shaper or needs_shaper()):
            return False, lines
        _say("Preparing SHAPER_CALIBRATE support (dedicated Python {} venv)"
             .format(PY_VERSION))
        interpreter = find_python314()
        if interpreter is None:
            if brew_available() and install_python:
                _say("Installing {} with Homebrew (this can take a few "
                     "minutes)...".format(BREW_PY))
                code, _, err = _run(["brew", "install", BREW_PY], BREW_TIMEOUT)
                if code != 0:
                    _say("[!] 'brew install {}' failed: {}".format(
                        BREW_PY, _brief(err)))
                interpreter = find_python314()
            if interpreter is None:
                _say("[!] Python {} is not installed, so SHAPER_CALIBRATE is "
                     "not available yet. When you want it: 'brew install {}' "
                     "(or python.org), then run setup again. The stack "
                     "itself works without it.".format(PY_VERSION, BREW_PY))
                return False, lines
        ok, messages = create_shaper_venv(interpreter=interpreter,
                                          verbose=verbose)
        lines.extend(messages)
        if not ok:
            _say("[!] Shaper venv is not ready; klippy stays on the main venv "
                 "and SHAPER_CALIBRATE will fail until it is.")
        return ok, lines
    except Exception as e:
        _say("[!] Shaper setup skipped ({}) — continuing without it".format(e))
        return False, lines


# Mirrors shaper_calibrate.py's real math path: windowing (as_strided), Welch
# PSD (kaiser + rfft), the numpy.exp low-frequency rolloff of
# CalibrationData.normalize_to_frequencies and ShaperCalibrate._estimate_shaper
# / fit_shaper array work — all without a printer, a config or an accelerometer.
SMOKE_SRC = '''
import math
import numpy as np

fs = 1000.
n = 2048
t = np.arange(n) / fs
x = np.sin(2. * math.pi * 35. * t) + .1 * np.sin(2. * math.pi * 120. * t)

nfft = 256
window = np.kaiser(nfft, 6.)
scale = 1. / (window ** 2).sum()
overlap = nfft // 2
step = nfft - overlap
n_windows = (x.shape[-1] - overlap) // step
wins = np.lib.stride_tricks.as_strided(
    x, shape=(nfft, n_windows),
    strides=(x.strides[-1], step * x.strides[-1]), writeable=False)
wins = window[:, None] * (wins - np.mean(wins, axis=0))
result = np.fft.rfft(wins, n=nfft, axis=0)
result = np.conjugate(result) * result
result *= scale / fs
result[1:-1, :] *= 2.
psd = result.real.mean(axis=-1)
freqs = np.fft.rfftfreq(nfft, 1. / fs)

freq_bins = freqs + .1
psd = psd / freq_bins
low = freq_bins < 2. * 5.
psd[low] *= np.exp(-(2. * 5. / (freq_bins[low] + .1)) ** 2 + 1.)

A = np.array([1., 1.])
T = np.array([0., 1. / (2. * 35.)])
dr = .1
omega = 2. * math.pi * freqs
W = A * np.exp(np.outer(-dr * omega, (T[-1] - T)))
S = W * np.sin(np.outer(omega * math.sqrt(1. - dr ** 2), T))
C = W * np.cos(np.outer(omega * math.sqrt(1. - dr ** 2), T))
vals = np.sqrt(S.sum(axis=1) ** 2 + C.sum(axis=1) ** 2) / A.sum()

test_freqs = np.arange(5., 100., .2)
best = np.zeros(shape=test_freqs.shape)
best = np.maximum(best, np.interp(test_freqs, freqs, vals))
peak = float(freqs[int(np.argmax(psd))])
print("numpy %s: shaper math path OK (peak %.1f Hz, %d bins, max score %.3f)"
      % (np.__version__, peak, best.shape[0], float(best.max())))
'''


def smoke_test():
    """(ok, output): prove the shaper venv can run klippy's numpy math path."""
    if not paths.SHAPER_PY.exists():
        return False, "shaper venv missing: {}".format(paths.SHAPER_VENV)
    code, out, err = _run([str(paths.SHAPER_PY), "-c", SMOKE_SRC],
                          SMOKE_TIMEOUT)
    combined = (out + err).strip()
    return code == 0 and "shaper math path OK" in out, combined
