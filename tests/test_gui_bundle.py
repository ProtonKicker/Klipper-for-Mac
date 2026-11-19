"""Packaging contract for the CLI bundled inside Klipper for Mac.app.

These tests keep the GUI independent of a separately installed `klipperformac`:
the launcher must find its own runtime relative to the app bundle, and the
packaging script must stage both supported architectures.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCE = ROOT / "KlipperMac" / "Resources" / "klipperformac"
BUNDLE_SCRIPT = ROOT / "scripts" / "bundle-cli.sh"
ENVIRONMENT_SOURCE = ROOT / "KlipperMac" / "Sources" / "KlipperMac" / "Core" / "Environment.swift"
HOST_ARCH = "arm64" if sys.platform == "darwin" and os.uname().machine == "arm64" else "x86_64"
RUNTIME_DIR = ROOT / ".build" / "pythons"
RUNTIMES = {
    "x86_64": RUNTIME_DIR / "cpython-3.12-macos-x86_64-none",
    "arm64": RUNTIME_DIR / "cpython-3.12-macos-aarch64-none",
}


def _clean_env():
    """No external CLI, no inherited Python hints — only the bundle can win."""
    return {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}


def test_bundled_launcher_uses_adjacent_runtime(tmp_path):
    """The launcher resolves its runtime beside itself, never from PATH."""
    assert LAUNCHER_SOURCE.is_file(), "GUI bundle needs a bundled CLI launcher"
    cli = tmp_path / "Contents" / "Resources" / "CLI"
    (cli / "bin").mkdir(parents=True)
    helper = cli.parent / "KlipperCam"
    helper.write_text("#!/bin/sh\\nexit 0\\n")
    helper.chmod(0o755)
    launcher = cli / "bin" / "klipperformac"
    shutil.copy2(LAUNCHER_SOURCE, launcher)
    launcher.chmod(0o755)
    (cli / "lib" / "klipperformac").mkdir(parents=True)
    (cli / "lib" / "klipperformac" / "__main__.py").write_text("")
    fake_python = cli / "runtimes" / HOST_ARCH / "bin" / "python3.12"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "ARGS=%s\\n" "$*"\n'
        'printf "PYTHONPATH=%s\\n" "$PYTHONPATH"\n'
        'printf "PY=%s\\n" "$0"\n'
        'printf "CAM=%s\\n" "$KLIPPERFORMAC_CAM_BIN"\n'
    )
    fake_python.chmod(0o755)

    proc = subprocess.run(
        [str(launcher), "--help"],
        capture_output=True,
        text=True,
        env=_clean_env(),
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ARGS=-m klipperformac --help" in proc.stdout
    assert f"PYTHONPATH={cli / 'lib'}" in proc.stdout
    assert str(fake_python) in proc.stdout
    assert f"CAM={helper}" in proc.stdout

    shutil.rmtree(cli / "runtimes" / HOST_ARCH)
    missing = subprocess.run(
        [str(launcher), "--help"], capture_output=True, text=True, env=_clean_env(), check=False
    )
    assert missing.returncode != 0
    assert "bundled Python" in missing.stderr


def test_stage_script_packages_cli_and_runtime(tmp_path):
    """Both architectures are staged with stdlib, headers and CLI sources."""
    if not all(p.is_dir() for p in RUNTIMES.values()):
        pytest.skip("download runtimes with scripts/fetch-python-runtimes.sh first")
    assert BUNDLE_SCRIPT.is_file(), "build-app.sh needs a CLI staging script"

    app = tmp_path / "Klipper for Mac.app"
    (app / "Contents").mkdir(parents=True)
    env = dict(os.environ)
    env["KLIPPERFORMAC_RUNTIME_X86_64"] = str(RUNTIMES["x86_64"])
    env["KLIPPERFORMAC_RUNTIME_ARM64"] = str(RUNTIMES["arm64"])
    subprocess.run([str(BUNDLE_SCRIPT), str(app)], env=env, check=True, capture_output=True)

    cli = app / "Contents" / "Resources" / "CLI"
    assert (cli / "bin" / "klipperformac").stat().st_mode & 0o111
    assert (cli / "lib" / "klipperformac" / "__main__.py").is_file()
    assert (cli / "lib" / "klipperformac" / "compat" / "include" / "linux" / "can.h").is_file()
    for arch, runtime in RUNTIMES.items():
        target = cli / "runtimes" / arch
        assert (target / "bin" / "python3.12").is_file()
        assert (target / "bin" / "python3").is_symlink()
        assert not (target / "lib" / "libpython3.12.dylib").exists()
        assert (target / "lib" / "python3.12" / "json").is_dir()
        assert (target / "include" / "python3.12" / "Python.h").is_file()
        assert (target / "lib" / "python3.12" / "ensurepip").is_dir()
        assert (target / "lib" / "python3.12" / "LICENSE.txt").is_file()
        assert not (target / "lib" / "python3.12" / "tkinter").exists()
        assert not list((target / "lib" / "python3.12" / "lib-dynload").glob("_tkinter*.so"))
        linked = subprocess.run(
            ["otool", "-L", str(target / "bin" / "python3.12")],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert str(runtime.resolve()) not in linked

    proc = subprocess.run(
        [str(cli / "bin" / "klipperformac"), "--help"],
        capture_output=True,
        text=True,
        env=_clean_env(),
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage: klipperformac" in proc.stdout


def test_gui_prefers_bundle_over_path():
    """The Swift GUI must resolve the in-app CLI without scanning PATH."""
    source = ENVIRONMENT_SOURCE.read_text()
    assert "Bundle.main.resourceURL" in source
    assert "CLI/bin/klipperformac" in source
    # A PATH scan was the old behavior; it must not be the primary resolution.
    resolver = source.split("static func resolveCLI", 1)[1].split("// MARK: - public", 1)[0]
    assert "resourceURL" in resolver
    assert 'environment["PATH"]' not in resolver


def test_agent_experiment_has_printer_recording_settings_panes():
    """The merged experimental app exposes all three requested panes."""
    source = (
        ROOT / "KlipperMac" / "Sources" / "KlipperMac" / "Views" / "ContentView.swift"
    ).read_text()
    assert 'case camera = "Recording"' in source
    assert "CameraView()" in source
    assert 'case printer = "Printer"' in source
    assert 'case settings = "Settings"' in source


def test_camera_helper_is_bundled_and_not_installed_globally():
    """Release build ships the helper in Resources without modifying HOME."""
    source = (ROOT / "KlipperMac" / "build-app.sh").read_text()
    assert 'install_cam "$APP/Contents/Resources/KlipperCam"' in source
    assert 'install_cam "$HOME/.klipperformac/bin/KlipperCam"' not in source


@pytest.mark.skipif(not os.environ.get("KLIPPERFORMAC_TEST_APP"), reason="no built app under test")
def test_built_app_verifier():
    """Run the full verifier against a built app (set KLIPPERFORMAC_TEST_APP)."""
    subprocess.run(
        [str(ROOT / "scripts" / "verify_bundle.py"), os.environ["KLIPPERFORMAC_TEST_APP"]],
        check=True,
    )
