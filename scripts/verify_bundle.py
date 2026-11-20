#!/usr/bin/env python3
"""Verify a built Klipper for Mac.app and its release ZIP are self-contained.

Run after `KLIPPERFORMAC_ZIP=1 ./KlipperMac/build-app.sh`. All checks are local
and offline; no stack setup, downloads or system changes are performed.
"""

import argparse
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "KlipperMac" / "dist" / "Klipper for Mac.app"
ZIP = ROOT / "KlipperMac" / "dist" / "Klipper-for-Mac.zip"
EXPECTED_RUNTIME_FLOORS = {"x86_64": "10.15", "arm64": "11.0"}
# GUI and camera helper are universal with per-arch deployment floors:
# Intel Catalina is supported (10.15) while Apple Silicon targets 11.0.
# Bundled CLI Python runtimes keep the same per-arch floors.
EXPECTED_GUI_FLOORS = {"x86_64": "10.15", "arm64": "11.0"}


def check(ok, message):
    print(("  ok: " if ok else "  FAIL: ") + message)
    if not ok:
        raise SystemExit(1)


def run(cmd, env=None, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, check=False)


def host_arch():
    machine = platform.machine()
    return "arm64" if machine in ("arm64", "aarch64") else "x86_64"


def minos(binary):
    if not shutil.which("vtool"):
        return []
    out = run(["vtool", "-show-build", str(binary)]).stdout
    return sorted({line.split()[1] for line in out.splitlines() if "minos" in line})


def clean_env(tmpdir):
    """PATH has decoy tools; only the bundle's files may satisfy the CLI."""
    bin_dir = Path(tmpdir) / "decoys"
    bin_dir.mkdir(exist_ok=True)
    for name in ("klipperformac", "python3", "python3.12"):
        decoy = bin_dir / name
        decoy.write_text("#!/bin/sh\necho 'external decoy used' >&2\nexit 99\n")
        decoy.chmod(0o755)
    return {
        "PATH": str(bin_dir) + ":/usr/bin:/bin",
        "HOME": str(Path(tmpdir) / "home"),
        "LANG": "en_US.UTF-8",
    }


def help_check(launcher, label, tmpdir):
    env = clean_env(tmpdir)
    proc = run([str(launcher), "--help"], env=env, cwd=tmpdir)
    check(
        proc.returncode == 0 and "usage: klipperformac" in proc.stdout,
        f"{label}: --help works with no external CLI/Python ({proc.returncode})",
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit(1)


def verify_app(app):
    check(app.is_dir(), f"app bundle exists: {app}")
    cli = app / "Contents" / "Resources" / "CLI"
    launcher = cli / "bin" / "klipperformac"
    check(
        launcher.is_file() and bool(launcher.stat().st_mode & stat.S_IXUSR),
        "bundled launcher is executable",
    )
    for rel in (
        "lib/klipperformac/__main__.py",
        "lib/klipperformac/compat/include/linux/can.h",
        "lib/klipperformac/compat/include/sys/prctl.h",
        "runtimes/x86_64/lib/python3.12/LICENSE.txt",
        "runtimes/arm64/lib/python3.12/LICENSE.txt",
        "README.txt",
    ):
        check((cli / rel).is_file(), f"bundled CLI file: {rel}")

    for arch, floor in EXPECTED_RUNTIME_FLOORS.items():
        root = cli / "runtimes" / arch
        python = root / "bin" / "python3.12"
        check(python.is_file(), f"{arch}: bundled python3.12")
        check((root / "bin" / "python3").is_symlink(), f"{arch}: python3 alias")
        check(
            not (root / "lib" / "libpython3.12.dylib").exists(),
            f"{arch}: static interpreter needs no external libpython",
        )
        check((root / "lib" / "python3.12" / "json").is_dir(), f"{arch}: stdlib")
        check(
            (root / "lib" / "python3.12" / "ensurepip").is_dir(), f"{arch}: venv/ensurepip support"
        )
        check(
            (root / "include" / "python3.12" / "Python.h").is_file(),
            f"{arch}: C headers for c_helper",
        )
        machos = [python, *root.rglob("*.so"), *root.rglob("*.dylib")]
        for binary in machos:
            label = binary.relative_to(root)
            floors = minos(binary)
            check(floors == [floor], f"{arch}: {label} minos {floors} == {floor}")
            if shutil.which("otool"):
                linked = run(["otool", "-L", str(binary)])
                dependencies = [
                    line.strip().split(" (")[0] for line in linked.stdout.splitlines()[1:]
                ]
                external = [
                    dep
                    for dep in dependencies
                    if not dep.startswith(("/usr/lib/", "/System/Library/"))
                ]
                check(
                    linked.returncode == 0 and not external,
                    f"{arch}: {label} links only system libraries {external}",
                )

    # GUI and camera helper are universal with per-arch deployment floors.
    gui = app / "Contents" / "MacOS" / "KlipperMac"
    check(gui.is_file(), "universal GUI binary exists")
    if shutil.which("lipo"):
        archs = set(run(["lipo", "-archs", str(gui)]).stdout.split())
        check(archs == {"arm64", "x86_64"}, f"GUI architectures: {sorted(archs)}")
    if shutil.which("file"):
        info = run(["file", str(gui)]).stdout.strip()
        check("universal" in info, f"file(1) reports {info}")
    check(
        sorted(minos(gui)) == sorted(EXPECTED_GUI_FLOORS.values()),
        f"GUI slices keep floors {sorted(EXPECTED_GUI_FLOORS.values())}: {minos(gui)}",
    )

    # Catalina (10.15) has no Swift Concurrency runtime in /usr/lib/swift.
    # The GUI weak-links @rpath/libswift_Concurrency.dylib and must ship the
    # back-deploy library so it can launch on Intel 10.15.
    conc = app / "Contents" / "Frameworks" / "libswift_Concurrency.dylib"
    check(conc.is_file(), "bundled Swift Concurrency back-deploy runtime for Catalina")
    if shutil.which("otool"):
        linked = run(["otool", "-L", str(gui)]).stdout
        check(
            "@rpath/libswift_Concurrency.dylib" in linked,
            "GUI weak-links @rpath/libswift_Concurrency.dylib",
        )
        rpaths = run(["otool", "-l", str(gui)]).stdout
        check("@executable_path/../Frameworks" in rpaths, "GUI has Frameworks rpath")

    camera_helper = app / "Contents" / "Resources" / "KlipperCam"
    check(camera_helper.is_file(), "bundled KlipperCam camera helper exists")
    check(
        sorted(minos(camera_helper)) == sorted(EXPECTED_GUI_FLOORS.values()),
        "KlipperCam slices keep floors "
        f"{sorted(EXPECTED_GUI_FLOORS.values())}: {minos(camera_helper)}",
    )

    if shutil.which("codesign"):
        signed = run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
        check(signed.returncode == 0, "codesign --verify --deep --strict")
        if signed.returncode != 0:
            print(signed.stdout[-2000:])
            print(signed.stderr[-2000:])

    arch = host_arch()
    with tempfile.TemporaryDirectory(prefix="klipperformac-bundle-") as tmpdir:
        help_check(launcher, f"app ({arch})", tmpdir)
    check(not list(cli.rglob("__pycache__")), "CLI smoke leaves no unsigned Python cache files")
    if shutil.which("codesign"):
        signed = run(["codesign", "--verify", "--deep", "--strict", str(app)])
        check(signed.returncode == 0, "codesign remains valid after CLI smoke")
        if signed.returncode != 0:
            print(signed.stderr[-2000:])

    size = run(["du", "-sh", str(app)]).stdout.strip().split()[0]
    print(f"  app size: {size}")
    return launcher


def verify_zip(zip_path, app_name):
    check(zip_path.is_file(), f"release ZIP exists: {zip_path}")
    prefix = app_name + "/"
    required = [
        prefix + "Contents/Info.plist",
        prefix + "Contents/MacOS/KlipperMac",
        prefix + "Contents/Resources/CLI/bin/klipperformac",
        prefix + "Contents/Resources/CLI/lib/klipperformac/__main__.py",
        prefix + "Contents/Resources/CLI/runtimes/x86_64/bin/python3.12",
        prefix + "Contents/Resources/CLI/runtimes/arm64/bin/python3.12",
        prefix + "Contents/Resources/CLI/runtimes/x86_64/include/python3.12/Python.h",
        prefix + "Contents/Resources/CLI/runtimes/arm64/include/python3.12/Python.h",
        prefix + "Contents/Resources/CLI/runtimes/x86_64/lib/python3.12/LICENSE.txt",
        prefix + "Contents/Resources/CLI/runtimes/arm64/lib/python3.12/LICENSE.txt",
    ]
    with zipfile.ZipFile(zip_path) as zf:
        infos = {info.filename: info for info in zf.infolist()}
        apple_double = [
            name for name in infos if Path(name).name.startswith("._") or "__MACOSX/" in name
        ]
        check(
            not apple_double, f"ZIP contains no AppleDouble metadata ({len(apple_double)} entries)"
        )
        for member in required:
            check(member in infos, f"ZIP member: {member}")
        launcher_info = infos[prefix + "Contents/Resources/CLI/bin/klipperformac"]
        check(
            bool(launcher_info.external_attr >> 16 & 0o111), "ZIP preserves launcher executable bit"
        )
        alias_path = prefix + "Contents/Resources/CLI/runtimes/x86_64/bin/python3"
        alias_info = infos[alias_path]
        check(stat.S_ISLNK(alias_info.external_attr >> 16), "ZIP records python3 as a symlink")
        check(zf.read(alias_path) == b"python3.12", "ZIP symlink points to python3.12")
        with tempfile.TemporaryDirectory(prefix="klipperformac-zip-") as tmpdir:
            if shutil.which("ditto"):
                extracted_result = run(["ditto", "-x", "-k", str(zip_path), tmpdir])
                check(extracted_result.returncode == 0, "macOS ditto extracts release ZIP")
            else:
                zf.extractall(tmpdir)
            extracted = Path(tmpdir) / prefix.rstrip("/")
            launcher = extracted / "Contents" / "Resources" / "CLI" / "bin" / "klipperformac"
            check(launcher.is_file(), "extracted app has a launcher")
            alias = (
                extracted
                / "Contents"
                / "Resources"
                / "CLI"
                / "runtimes"
                / "x86_64"
                / "bin"
                / "python3"
            )
            check(
                alias.is_symlink() and alias.readlink().as_posix() == "python3.12",
                "extracted ZIP preserves Python symlink",
            )
            if shutil.which("codesign"):
                signed = run(["codesign", "--verify", "--deep", "--strict", str(extracted)])
                check(signed.returncode == 0, "extracted ZIP preserves the app signature")
                if signed.returncode != 0:
                    print(signed.stderr[-2000:])
            # Python's zipfile does not restore file modes; macOS Archive
            # Utility / ditto / unzip do. Mirror that only for the fallback.
            if not shutil.which("ditto"):
                launcher.chmod(0o755)
                for arch in EXPECTED_RUNTIME_FLOORS:
                    (
                        extracted
                        / "Contents"
                        / "Resources"
                        / "CLI"
                        / "runtimes"
                        / arch
                        / "bin"
                        / "python3.12"
                    ).chmod(0o755)
            help_check(launcher, f"extracted ZIP ({host_arch()})", tmpdir)
    print(f"  ZIP size: {zip_path.stat().st_size / 1024 / 1024:.1f} MiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="?", default=str(APP), help="path to built .app")
    parser.add_argument("--zip", default=str(ZIP), help="release ZIP to verify")
    parser.add_argument("--no-zip", action="store_true", help="skip ZIP checks")
    args = parser.parse_args()
    app = Path(args.app).resolve()
    verify_app(app)
    if not args.no_zip:
        verify_zip(Path(args.zip).resolve(), app.name)
    print("PASS: bundled CLI is self-contained and matches the release ZIP")


if __name__ == "__main__":
    sys.exit(main())
