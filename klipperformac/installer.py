"""Fetches pinned upstream components, builds the venv, and proves the shim
compile works.

Pristine-upstream attestation: checkouts live under ~/.klipperformac, marker
files live outside them (refs.d/), build artifacts are added to
.git/info/exclude, and setup/doctor verify `git status --porcelain` is
empty for every checkout.

Failure policy: upstream trees are always fetched into a staging directory and
swapped into place afterwards (a failed clone/download must never cost the user
a working install), and every expected failure leaves as SystemExit: a short CLI
error the user can act on, never a traceback.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import configgen, paths, shaper
from .compat import build_env, ensure_runtime_shims, invalidate_stale_build
from .versions import DEFAULT_PINS, REPOS, load_pins, save_pins

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

# The only pins lockfile.json may steer: each one becomes a `git --branch` /
# `git fetch` argument or a release-asset URL path segment.
PIN_KEYS = ("klipper", "moonraker", "mainsail", "fluidd")

# What git itself will accept as a remote ref/commit, minus anything that
# could be read as an option or a URL trick (no space, control byte, ':',
# '?', '*', '[', '\\', '@{', leading '-').
_REF_CHARS = ("abcdefghijklmnopqrstuvwxyz"
              "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
              "0123456789._+/-")
_REF_SHAPE = re.compile("[A-Za-z0-9][" + re.escape(_REF_CHARS) + "]*")
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
# APFS/HFS+ NAME_MAX: a longer single name fails as ENAMETOOLONG, so archives
# that claim one are refused as a user error instead of half-extracted.
_MAX_NAME = 255


def _run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def _detail(exc):
    """Bounded, last-lines-only explanation of a failed child process/OSError."""
    stream = getattr(exc, "stderr", None)
    if stream is None:
        stream = getattr(exc, "output", None)
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", "replace")
    if not stream:
        stream = str(exc)
    lines = [ln for ln in stream.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-5:])[-500:] or "unknown error"


def _validate_ref(component, ref):
    """lockfile.json is user-editable input: validate the shape of every ref
    before it reaches git, a URL, or the filesystem."""
    if not isinstance(ref, str):
        raise SystemExit(
            "Invalid {} pin {!r}: expected a git tag, branch name or commit id "
            "(a string). Edit {}, or delete that key to use the shipped "
            "pin.".format(component, ref, paths.LOCKFILE))
    ref = ref.strip()
    if (not ref or len(ref) > _MAX_NAME or ".." in ref
            or not _REF_SHAPE.fullmatch(ref)):
        raise SystemExit(
            "Invalid {} pin {!r}: expected a git tag, branch name or 40-hex "
            "commit id (no spaces, control characters or '..'). Edit {}, or "
            "delete that key to use the shipped pin.".format(
                component, ref, paths.LOCKFILE))
    return ref


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
                exe = _run([c, "-c", "import sys; print(sys.executable)"],
                           capture_output=True).stdout.decode().strip()
                # A wrapper/shim that answers the version probe but not the
                # executable probe is not a usable interpreter: '' would be
                # exec'd as ['', '-m', 'venv', ...] further down.
                if exe:
                    return exe
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


def _require_head_sha(component, ref, dest, pinned=""):
    """The commit actually on disk. An empty sha must never be persisted as
    klipper_sha='': `update` then stops comparing against upstream master.
    `pinned` is the 40-hex id the ref names, which needs no rev-parse."""
    sha = _head_sha(dest) or pinned
    if not sha:
        raise SystemExit(
            "Could not read the installed commit of {} ({} at {}): "
            "`git rev-parse HEAD` reported nothing, so this checkout is not "
            "the {} we pinned. Re-run: klipperformac setup".format(
                component, dest, ref, component))
    return sha


def _exclude_build_artifacts(dest):
    """Keep the working tree 'git status' clean despite klipper compiling
    c_helper.so inside it (upstream behavior we do not modify)."""
    excl = Path(dest) / ".git" / "info" / "exclude"
    try:
        excl.parent.mkdir(parents=True, exist_ok=True)
        existing = excl.read_text() if excl.exists() else ""
        # Line-wise: a substring test cannot tell a live pattern from a
        # comment that merely mentions one, and a blind append would glue the
        # first pattern onto a last line that has no trailing newline.
        lines = existing.splitlines()
        missing = [p for p in GIT_EXCLUDE if p not in lines]
        if missing:
            with open(excl, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n".join(missing) + "\n")
    except OSError:
        pass


def _swap_in(src, dest, what):
    """Put the freshly fetched tree at `src` where `dest` is, keeping the tree
    it replaces restorable until the replacement is actually in position."""
    if dest.is_symlink():
        raise SystemExit("Refusing to replace symlink at {}".format(dest))
    old = None
    if dest.exists():
        old = dest.parent / (dest.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        try:
            os.rename(dest, old)
        except OSError as exc:
            raise SystemExit("Could not move the installed {} aside at {}: "
                             "{}".format(what, dest, exc))
    try:
        os.rename(src, dest)
    except OSError as exc:
        if old is not None:
            try:
                # Roll the previous tree straight back into place. shutil.move
                # rather than a bare rename: it falls back to copying when an
                # atomic rename is not available (separate volume, EBUSY), so a
                # failed swap can never leave the web root/checkout missing.
                shutil.move(str(old), str(dest))
            except OSError:
                pass
        shutil.rmtree(src, ignore_errors=True)
        raise SystemExit(
            "Could not install {} at {}: {}. The previous version is still in "
            "place; free up disk space and re-run: klipperformac setup".format(
                what, dest, exc))
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def checkout(component, ref, force=False):
    dest = DESTS[component]
    ref = _validate_ref(component, ref)
    pinned = ref.lower() if _FULL_SHA.fullmatch(ref) else ""
    if dest.is_symlink():
        raise SystemExit("Refusing to replace symlink at {}".format(dest))
    marker = _ref_marker(component)
    if not force and marker.exists() and marker.read_text().strip() == ref \
            and dest.exists():
        return _require_head_sha(component, ref, dest, pinned)
    url = URLS[component]
    stage = dest.parent / (dest.name + ".new")
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    # Fetch into a staging tree first: an offline `update --apply`, a yanked
    # tag or a GitHub outage must never leave the user without the checkout
    # that was working a moment ago.
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _FULL_SHA.fullmatch(ref):
            # git clone --branch rejects raw SHAs; shallow-fetch the exact
            # commit (git's own commit ids are case-insensitive).
            _run(["git", "init", "--quiet", str(stage)], check=True)
            _run(["git", "-C", str(stage), "remote", "add", "origin", url],
                 check=True)
            _run(["git", "-C", str(stage), "fetch", "--quiet", "--depth", "1",
                  "origin", pinned], check=True)
            _run(["git", "-C", str(stage), "checkout", "--quiet", "--detach",
                  "FETCH_HEAD"], check=True)
        else:
            _run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref,
                  url, str(stage)], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise SystemExit(
            "Could not fetch {} @ {} from upstream ({}).\n{}\nYour existing {} "
            "checkout at {} was left untouched.".format(
                component, ref, url, _detail(exc), component, dest))
    _exclude_build_artifacts(stage)
    _swap_in(stage, dest, component)
    marker.write_text(ref + "\n")
    return _require_head_sha(component, ref, dest, pinned)


def verify_pristine(component=None):
    """Upstream checkouts must contain no tracked modifications."""
    comps = [component] if component else list(DESTS)
    bad = []
    for comp in comps:
        dest = DESTS.get(comp)
        if dest is None:
            raise SystemExit("Unknown component: {} (expected one of: {})".format(
                comp, ", ".join(sorted(DESTS))))
        if not (dest / ".git").exists():
            continue
        try:
            res = _run(["git", "-C", str(dest), "status", "--porcelain"],
                       capture_output=True)
        except OSError as exc:
            bad.append((comp, "git status could not run: {}".format(exc)))
            continue
        out = res.stdout.decode("utf-8", "replace").strip()
        if res.returncode != 0:
            # A checkout git refuses to inspect is NOT a pristine one: dubious
            # ownership, a corrupted index or a broken .git would otherwise be
            # reported as "[ok] upstream checkouts pristine".
            bad.append((comp, "git status failed (exit {}): {}".format(
                res.returncode, _detail(res))))
        elif out:
            bad.append((comp, out))
    return bad


def create_venv():
    if not paths.VENV_PY.exists():
        py = find_python()
        if not py:
            raise SystemExit("No suitable Python found (see setup error above)")
        try:
            _run([py, "-m", "venv", str(paths.APP_HOME / "venv")], check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise SystemExit(
                "Could not create the Python venv in {}.\n{}\nInstall a working "
                "Python 3.12 (brew install python@3.12), make sure the folder "
                "is writable and has free space, then re-run: klipperformac "
                "setup".format(paths.APP_HOME / "venv", _detail(exc)))
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
                try:
                    _run([str(paths.VENV_PY), "-m", "pip", "install",
                          "--quiet", "--upgrade", "-r", str(req)],
                         check=True)
                except (subprocess.CalledProcessError, OSError) as exc:
                    raise SystemExit(
                        "Dependency install failed for {} ({}).\n{}\nThe "
                        "checkout is untouched; fix the reported conflict, "
                        "then re-run: klipperformac setup".format(
                            comp, req.name, _detail(exc)))
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
    """Extract only entries that stay inside target (zip-slip guard).

    Every member name is audited before anything is written, and a name the
    guard cannot vouch for is refused the same way an escaping path is, so
    extraction never writes a different path from the one that was checked.
    """
    base = os.path.realpath(str(target))
    for m in z.infolist():
        name = m.filename
        # Check the name itself before letting it near the filesystem: an
        # embedded NUL is not a path, and os.path.realpath() rejects one.
        if "\x00" in name:
            raise SystemExit("Unsafe zip path: " + name)
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(len(p.encode("utf-8", "replace")) > _MAX_NAME for p in parts):
            raise SystemExit("Unsafe zip path: " + name)
        dest = os.path.realpath(os.path.join(base, name))
        if dest == base:
            # '.', './' and 'sub/' members name the target itself; a name that
            # walks up ('aaaaaaaa/..') still does not count as inside.
            if ".." in parts:
                raise SystemExit("Unsafe zip path: " + name)
            continue
        if not dest.startswith(base + os.sep):
            raise SystemExit("Unsafe zip path: " + name)
    try:
        z.extractall(base)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        member = getattr(exc, "filename", None) or "the archive"
        raise SystemExit("Cannot extract {} from the downloaded archive: {}. "
                         "The release asset is malformed; re-run: klipperformac "
                         "setup".format(member, _detail(exc)))


def fetch_webui(component, tag, dest):
    if dest.is_symlink():
        raise SystemExit("Refusing to replace symlink at {}".format(dest))
    tag = _validate_ref(component, tag)
    marker = _ref_marker(component)
    if marker.exists() and marker.read_text().strip() == tag and dest.exists():
        return
    asset_name = component + ".zip"
    url = "https://github.com/{}/releases/download/{}/{}".format(
        REPOS[component], tag, asset_name)
    fd, name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)                      # _download() opens the path itself
    zip_path = Path(name)
    try:
        try:
            _download(url, zip_path)
        except (OSError, ValueError) as exc:
            raise SystemExit(
                "Could not download {} — check your network connection (and "
                "that a VPN or captive portal is not blocking GitHub).\n{}"
                "\nNothing was installed; re-run: klipperformac setup".format(
                    url, _detail(exc)))
        try:
            archive = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            raise SystemExit(
                "The downloaded archive is not a valid zip file ({} from {})."
                "\n{}\nGitHub may be serving an error page; re-run: "
                "klipperformac setup".format(asset_name, url, _detail(exc)))
        with archive, tempfile.TemporaryDirectory() as td:
            _safe_extract(archive, td)
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
            _swap_in(stage, dest, asset_name)
        marker.write_text(tag + "\n")
    finally:
        zip_path.unlink(missing_ok=True)


def _patch_ui_config(component, dest):
    """Point the UIs at their own proxied origin: klipperformac.proxy relays
    REST + websocket to Moonraker on the same port, so defaults work."""
    if component not in ("mainsail", "fluidd"):
        return                      # never rewrite a file we do not understand
    cfg = dest / "config.json"
    try:
        data = json.loads(cfg.read_text())
    except Exception:
        return
    if not isinstance(data, dict):
        # Valid JSON, but not a key/value config: leave the asset alone rather
        # than crash setup after the tree has already been staged.
        return
    if component == "mainsail":
        data["hostname"] = None
        data["port"] = None
    elif component == "fluidd":
        data["socket_url"] = "ws://localhost:{}/websocket".format(
            paths.FLUIDD_PORT)
    try:
        cfg.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        return                      # a read-only asset must not be fatal


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


def setup(verbose=True, with_shaper=False, install_python=False):
    try:
        pins = load_pins()
    except (OSError, TypeError, ValueError):
        # A truncated or non-object lockfile.json (a kill or a power loss in
        # the previous save_pins) must not wedge setup: the shipped pins are
        # re-written below, which repairs the file.
        pins = dict(DEFAULT_PINS)
    # Validate before anything is re-saved or fetched: setup() must not
    # persist a pin it is about to hand to git as an argument.
    for key in PIN_KEYS:
        pins[key] = _validate_ref(key, pins.get(key))
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
        raise SystemExit(
            "Upstream checkouts are not pristine -- this project only ever "
            "runs unmodified Klipper and Moonraker sources, so setup stopped:"
            "\n" + "\n".join("  {}: {}".format(comp, out) for comp, out in bad))
    elif verbose:
        print("  upstream checkouts verified pristine (git status clean)")
    shaper.ensure_shaper(with_shaper=with_shaper, install_python=install_python,
                         verbose=verbose)
    if verbose:
        print("\nAll components fetched from upstream and verified to build on "
              "this Mac.\nNext: connect your printer board, run `klipperformac "
              "serial`, then `klipperformac up`.")
    return pins
