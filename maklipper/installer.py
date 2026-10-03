"""Fetches pinned upstream components, builds the venv, and proves the shim
compile works — Klipper sources stay pristine, verified by git."""
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from . import configgen, paths
from .compat import build_env
from .versions import REPOS, load_pins, save_pins

MIN_PY = (3, 10)
PY_CANDIDATES = ["python3.14", "python3.13", "python3.12", "python3.11", "python3"]
URLS = {
    "klipper": "https://github.com/Klipper3d/klipper",
    "moonraker": "https://github.com/Arksine/moonraker",
}
DESTS = {"klipper": paths.KLIPPER, "moonraker": paths.MOONRAKER}


def _run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def check_prereqs(strict=True):
    problems = []
    for tool in ("git", "gcc"):
        if shutil.which(tool) is None:
            problems.append(
                "'{}' not found. Install Xcode Command Line Tools: "
                "xcode-select --install".format(tool)
            )
    if find_python() is None:
        problems.append(
            "No Python >= {} found. Try: brew install python@3.12".format(
                ".".join(map(str, MIN_PY))
            )
        )
    if problems and strict:
        raise SystemExit("MaKlipper setup requires:\n  - " + "\n  - ".join(problems))
    return problems


def find_python():
    env = __import__("os").environ.get("MAKLIPPER_PYTHON")
    candidates = ([env] if env else []) + [
        c for c in PY_CANDIDATES if shutil.which(c)
    ]
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


def checkout(component, ref, force=False):
    dest = DESTS[component]
    marker = dest / ".maklipper-ref"
    if not force and marker.exists() and marker.read_text().strip() == ref:
        return _head_sha(dest)
    if dest.exists():
        shutil.rmtree(dest)
    url = URLS[component]
    _run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref, url, str(dest)],
         check=True)
    marker.write_text(ref + "\n")
    return _head_sha(dest)


def _head_sha(dest):
    try:
        return _run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                    capture_output=True).stdout.decode().strip()
    except Exception:
        return ""


def create_venv():
    if paths.VENV_PY.exists():
        return
    py = find_python()
    if py is None:
        raise SystemExit("No suitable Python found (see setup error above)")
    _run([py, "-m", "venv", str(paths.APP_HOME / "venv")], check=True)


REQ_CANDIDATES = {
    "klipper": ["scripts/klippy-requirements.txt", "klippy/requirements.txt"],
    "moonraker": ["scripts/moonraker-requirements.txt", "requirements.txt"],
}


def install_requirements():
    pip = str(paths.APP_HOME / "venv" / "bin" / "pip")
    for comp, cands in REQ_CANDIDATES.items():
        for rel in cands:
            req = DESTS[comp] / rel
            if req.exists():
                _run([pip, "install", "--quiet", "--upgrade", "-r", str(req)],
                     check=True)
                break
        else:
            raise SystemExit(
                "No requirements file found in {} checkout".format(comp))


def fetch_mainsail(tag):
    import urllib.request
    from .versions import _get_json
    marker = paths.WEB / ".maklipper-ref"
    if marker.exists() and marker.read_text().strip() == tag:
        return
    info = _get_json(
        "https://api.github.com/repos/{}/releases/tags/{}".format(REPOS["mainsail"], tag)
    )
    asset = next((a for a in info["assets"] if a["name"] == "mainsail.zip"), None)
    if asset is None:
        raise SystemExit("No mainsail.zip asset on release {}".format(tag))
    zip_path = Path(tempfile.mkstemp(suffix=".zip")[1])
    try:
        urllib.request.urlretrieve(asset["browser_download_url"], zip_path)
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(td)
            root = None
            for p in Path(td).rglob("index.html"):
                root = p.parent
                break
            if root is None:
                raise SystemExit("mainsail.zip missing index.html")
            if paths.WEB.exists():
                shutil.rmtree(paths.WEB)
            paths.WEB.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(root, paths.WEB)
        marker.write_text(tag + "\n")
    finally:
        zip_path.unlink(missing_ok=True)


def test_chelper_build():
    """Compile + load c_helper.so exactly the way klippy will at first run."""
    script = (
        "import sys; sys.path.insert(0, {klippy!r}); "
        "import chelper; chelper.get_ffi(); print('c_helper built and loaded')"
    ).format(klippy=str(paths.KLIPPER / "klippy"))
    res = _run([str(paths.VENV_PY), "-c", script], env=build_env(),
               capture_output=True)
    if res.returncode != 0:
        detail = res.stderr.decode()[-1500:]
        raise RuntimeError(
            "c_helper.so build FAILED with upstream Klipper + MaKlipper "
            "compat shims.\n" + detail
        )
    return True


def setup(verbose=True):
    pins = load_pins()
    save_pins(pins)
    problems = check_prereqs()
    if problems:
        raise SystemExit("MaKlipper setup requires:\n  - " + "\n  - ".join(problems))
    if verbose:
        print("Fetching upstream components (pinned tags, no patches)...")
    for comp in ("klipper", "moonraker"):
        sha = checkout(comp, pins[comp])
        pins[comp + "_sha"] = sha
        if verbose:
            print("  {} @ {} ({})".format(comp, pins[comp], sha))
    save_pins(pins)
    create_venv()
    install_requirements()
    fetch_mainsail(pins["mainsail"])
    if verbose:
        print("  mainsail @ {}".format(pins["mainsail"]))
    created = configgen.ensure_configs()
    for c in created:
        if verbose:
            print("  created {}".format(c))
    test_chelper_build()
    if verbose:
        print("\nAll components fetched from upstream and verified to build on "
              "this Mac.\nNext: connect your printer board, run `maklipper serial`,"
              " then `maklipper up`.")
    return pins
