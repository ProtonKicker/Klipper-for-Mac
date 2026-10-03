"""Fetches pinned upstream components, builds the venv, and proves the shim
compile works.

Pristine-upstream attestation: checkouts live under ~/.klipperformac, marker
files live outside them (refs.d/), build artifacts are added to
.git/info/exclude, and setup/doctor verify `git status --porcelain` is
empty for every checkout.
"""
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import configgen, paths
from .compat import build_env, ensure_runtime_shims, invalidate_stale_build
from .versions import REPOS, load_pins, save_pins

MIN_PY = (3, 10)
PY_CANDIDATES = ["python3.14", "python3.13", "python3.12", "python3.11", "python3"]
URLS = {
    "klipper": "https://github.com/Klipper3d/klipper",
    "moonraker": "https://github.com/Arksine/moonraker",
}
DESTS = {"klipper": paths.KLIPPER, "moonraker": paths.MOONRAKER}
REFS = paths.APP_HOME / "refs.d"

GIT_EXCLUDE = ("c_helper.so", "c_helper.so.dSYM", "_temp_c_helper.so*",
               "klippy.log", "__pycache__/")


def _run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def check_prereqs(strict=True):
    problems = []
    for tool in ("git", "gcc"):
        if shutil.which(tool) is None:
            problems.append(
                "'{}' not found. Install Xcode Command Line Tools: "
                "xcode-select --install".format(tool))
    if find_python() is None:
        problems.append(
            "No Python >= {} found. Try: brew install python@3.12".format(
                ".".join(map(str, MIN_PY))))
    if problems and strict:
        raise SystemExit("Klipper for Mac setup requires:\n  - " + "\n  - ".join(problems))
    return problems


def find_python():
    env = os.environ.get("KLIPPERFORMAC_PYTHON")
    candidates = ([env] if env else []) + [
        c for c in PY_CANDIDATES if shutil.which(c)]
    for c in candidates:
        try:
            out = _run([c, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                       capture_output=True).stdout.decode().strip()
            if tuple(int(x) for x in out.split(".")) >= MIN_PY:
                return _run([c, "-c", "import sys; print(sys.executable)"],
                            capture_output=True).stdout.decode().strip()
        except Exception:
            continue
    return None


def _ref_marker(component):
    REFS.mkdir(parents=True, exist_ok=True)
    return REFS / (component + ".ref")


def _head_sha(dest):
    try:
        return _run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                    capture_output=True).stdout.decode().strip()
    except Exception:
        return ""


def _exclude_build_artifacts(dest):
    """Keep the working tree 'git status' clean despite klipper compiling
    c_helper.so inside it (upstream behavior we do not modify)."""
    excl = Path(dest) / ".git" / "info" / "exclude"
    try:
        excl.parent.mkdir(parents=True, exist_ok=True)
        existing = excl.read_text() if excl.exists() else ""
        missing = [p for p in GIT_EXCLUDE if p not in existing]
        if missing:
            with open(excl, "a") as f:
                f.write("\n".join(missing) + "\n")
    except OSError:
        pass


def checkout(component, ref, force=False):
    dest = DESTS[component]
    marker = _ref_marker(component)
    if not force and marker.exists() and marker.read_text().strip() == ref \
            and dest.exists():
        return _head_sha(dest)
    if dest.is_symlink():
        raise SystemExit("Refusing to replace symlink at {}".format(dest))
    if dest.exists():
        shutil.rmtree(dest)
    url = URLS[component]
    _run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref, url, str(dest)],
         check=True)
    _exclude_build_artifacts(dest)
    marker.write_text(ref + "\n")
    return _head_sha(dest)


def verify_pristine(component=None):
    """Upstream checkouts must contain no tracked modifications."""
    comps = [component] if component else list(DESTS)
    bad = []
    for comp in comps:
        dest = DESTS.get(comp)
        if not dest or not (dest / ".git").exists():
            continue
        out = _run(["git", "-C", str(dest), "status", "--porcelain"],
                   capture_output=True).stdout.decode().strip()
        if out:
            bad.append((comp, out))
    return bad


def create_venv():
    if not paths.VENV_PY.exists():
        py = find_python()
        if py is None:
            raise SystemExit("No suitable Python found (see setup error above)")
        _run([py, "-m", "venv", str(paths.APP_HOME / "venv")], check=True)
    ensure_runtime_shims(paths.VENV_PY)


REQ_CANDIDATES = {
    "klipper": ["scripts/klippy-requirements.txt", "klippy/requirements.txt"],
    "moonraker": ["scripts/moonraker-requirements.txt", "requirements.txt"],
}


def install_requirements():
    # NB: `-m pip`, not venv/bin/pip: the console script's shebang breaks if
    # the venv is ever relocated, the interpreter symlink does not.
    for comp, cands in REQ_CANDIDATES.items():
        for rel in cands:
            req = DESTS[comp] / rel
            if req.exists():
                _run([str(paths.VENV_PY), "-m", "pip", "install",
                      "--quiet", "--upgrade", "-r", str(req)],
                     check=True)
                break
        else:
            raise SystemExit("No requirements file found in {} checkout".format(comp))


def test_libsodium():
    """Moonraker's auth component hard-requires libsodium (via libnacl)."""
    res = _run([str(paths.VENV_PY), "-c", "import libnacl"], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(
            "libsodium missing — Moonraker will not start.\n"
            "Install it with:  brew install libsodium")


def _download(url, dest_path, timeout=60, max_bytes=200 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": "klipperformac"})
    with urllib.request.urlopen(req, timeout=timeout) as r, \
            open(dest_path, "wb") as f:
        read = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            read += len(chunk)
            if read > max_bytes:
                raise SystemExit("Download exceeded size cap: " + url)
            f.write(chunk)


def _safe_extract(z, target):
    """Extract only entries that stay inside target (zip-slip guard)."""
    target = str(Path(target).resolve()) + os.sep
    for m in z.infolist():
        dest = os.path.realpath(os.path.join(target, m.filename))
        if not dest.startswith(target):
            raise SystemExit("Unsafe zip path: " + m.filename)
    z.extractall(target)


def fetch_webui(component, tag, dest):
    marker = _ref_marker(component)
    if marker.exists() and marker.read_text().strip() == tag and dest.exists():
        return
    asset_name = component + ".zip"
    url = "https://github.com/{}/releases/download/{}/{}".format(
        REPOS[component], tag, asset_name)
    zip_path = Path(tempfile.mkstemp(suffix=".zip")[1])
    try:
        _download(url, zip_path)
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(zip_path) as z:
                _safe_extract(z, td)
            root = None
            for p in Path(td).rglob("index.html"):
                root = p.parent
                break
            if root is None:
                raise SystemExit(asset_name + " missing index.html")
            stage = dest.parent / (dest.name + ".new")
            if stage.exists():
                shutil.rmtree(stage)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(root, stage)
            _patch_ui_config(component, stage)
            (stage / ".klipperformac-ref").write_text(tag + "\n")
            old = None
            if dest.exists():
                old = dest.parent / (dest.name + ".old")
                if old.exists():
                    shutil.rmtree(old)
                os.rename(dest, old)
            os.rename(stage, dest)
            if old is not None:
                shutil.rmtree(old, ignore_errors=True)
        marker.write_text(tag + "\n")
    finally:
        zip_path.unlink(missing_ok=True)


def _patch_ui_config(component, dest):
    """Point the UIs at their own proxied origin: klipperformac.proxy relays
    REST + websocket to Moonraker on the same port, so defaults work."""
    cfg = dest / "config.json"
    try:
        data = json.loads(cfg.read_text())
    except Exception:
        return
    if component == "mainsail":
        data["hostname"] = None
        data["port"] = None
    elif component == "fluidd":
        data["socket_url"] = "ws://localhost:{}/websocket".format(
            paths.FLUIDD_PORT)
    cfg.write_text(json.dumps(data, indent=2) + "\n")


def fetch_mainsail(tag):
    fetch_webui("mainsail", tag, paths.WEB_MAINSAIL)


def fetch_fluidd(tag):
    fetch_webui("fluidd", tag, paths.WEB_FLUIDD)


def test_chelper_build():
    """Compile + load c_helper.so exactly the way klippy will at first run."""
    invalidate_stale_build(paths.KLIPPER)
    script = (
        "import sys; sys.path.insert(0, {klippy!r}); "
        "import chelper; chelper.get_ffi(); print('c_helper built and loaded')"
    ).format(klippy=str(paths.KLIPPER / "klippy"))
    res = _run([str(paths.VENV_PY), "-c", script], env=build_env(),
               capture_output=True)
    if res.returncode != 0:
        detail = res.stderr.decode()[-1500:]
        raise RuntimeError(
            "c_helper.so build FAILED with upstream Klipper + Klipper for Mac "
            "compat shims.\n" + detail)


def setup(verbose=True):
    pins = load_pins()
    save_pins(pins)
    problems = check_prereqs()
    if problems:
        raise SystemExit("Klipper for Mac setup requires:\n  - " + "\n  - ".join(problems))
    if verbose:
        print("Fetching upstream components (pristine checkouts, pinned refs)...")
    for comp in ("klipper", "moonraker"):
        sha = checkout(comp, pins[comp])
        pins[comp + "_sha"] = sha
        if verbose:
            print("  {} @ {} ({})".format(comp, pins[comp], sha))
    save_pins(pins)
    create_venv()
    install_requirements()
    test_libsodium()
    fetch_mainsail(pins["mainsail"])
    fetch_fluidd(pins["fluidd"])
    if verbose:
        print("  mainsail @ {}  fluidd @ {}".format(
            pins["mainsail"], pins["fluidd"]))
    created = configgen.ensure_configs()
    for c in created:
        if verbose:
            print("  created {}".format(c))
    test_chelper_build()
    bad = verify_pristine()
    if bad:
        for comp, out in bad:
            print("[!] {} checkout is not pristine:\n{}".format(comp, out))
    elif verbose:
        print("  upstream checkouts verified pristine (git status clean)")
    if verbose:
        print("\nAll components fetched from upstream and verified to build on "
              "this Mac.\nNext: connect your printer board, run `klipperformac "
              "serial`, then `klipperformac up`.")
    return pins
