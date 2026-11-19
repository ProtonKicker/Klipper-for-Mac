#!/bin/sh
# Stage a self-contained CLI inside an assembled Klipper for Mac.app.
#
# Usage: scripts/bundle-cli.sh "/path/to/Klipper for Mac.app"
#
# Runtime roots can be overridden with KLIPPERFORMAC_RUNTIME_X86_64 and
# KLIPPERFORMAC_RUNTIME_ARM64 (directories from fetch-python-runtimes.sh).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP=${1:-"$ROOT/KlipperMac/dist/Klipper for Mac.app"}
CLI="$APP/Contents/Resources/CLI"

die() { echo "bundle-cli: $*" >&2; exit 1; }

check_floor() {
    arch=$1
    binary=$2
    expected=$3
    command -v vtool >/dev/null 2>&1 || return 0
    actual=$(vtool -show-build "$binary" 2>/dev/null | awk '/minos/{print $2}' | sort -u)
    [ -n "$actual" ] || die "cannot read deployment target of $binary"
    if printf '%s\n' "$actual" | grep -vxq "$expected"; then
        die "$arch runtime has minos $actual, expected $expected"
    fi
}

stage_runtime() {
    arch=$1
    src=$2
    expected=$3
    [ -d "$src" ] || die "$arch runtime missing at $src (run scripts/fetch-python-runtimes.sh)"
    src=$(CDPATH= cd -- "$src" && pwd -P)
    [ -x "$src/bin/python3.12" ] || die "no bin/python3.12 in $src"
    [ -f "$src/include/python3.12/Python.h" ] || die "no Python.h in $src (setup compiles c_helper)"
    check_floor "$arch" "$src/bin/python3.12" "$expected"

    target="$CLI/runtimes/$arch"
    rm -rf "$target"
    mkdir -p "$target/bin" "$target/lib" "$target/include"
    cp "$src/bin/python3.12" "$target/bin/python3.12"
    chmod 755 "$target/bin/python3.12"
    ln -s python3.12 "$target/bin/python3"
    cp -R "$src/lib/python3.12" "$target/lib/"
    cp -R "$src/include/python3.12" "$target/include/"

    # Trim pieces the CLI never imports (tens of MB across both architectures).
    for extra in tkinter turtledemo idlelib lib2to3 site-packages; do
        rm -rf "$target/lib/python3.12/$extra"
    done
    rm -f "$target/lib/python3.12/lib-dynload/_tkinter"*.so
    find "$target" -type d -name __pycache__ -prune -exec rm -rf {} +
    find "$target" -type f -name '*.pyc' -delete
}

[ -d "$APP/Contents" ] || die "app bundle not assembled yet: $APP"

X86=${KLIPPERFORMAC_RUNTIME_X86_64:-"$ROOT/.build/pythons/cpython-3.12-macos-x86_64-none"}
ARM=${KLIPPERFORMAC_RUNTIME_ARM64:-"$ROOT/.build/pythons/cpython-3.12-macos-aarch64-none"}

echo "==> bundling CLI into $CLI"
rm -rf "$CLI"
mkdir -p "$CLI/bin" "$CLI/lib"
cp "$ROOT/KlipperMac/Resources/klipperformac" "$CLI/bin/klipperformac"
chmod 755 "$CLI/bin/klipperformac"
cp -R "$ROOT/klipperformac" "$CLI/lib/klipperformac"
rm -rf "$CLI/lib/klipperformac/tests"
find "$CLI/lib" -type d -name __pycache__ -prune -exec rm -rf {} +

stage_runtime x86_64 "$X86" 10.15
stage_runtime arm64 "$ARM" 11.0

{
    printf 'Klipper for Mac — bundled CLI\n'
    printf 'Python: %s\n' "$("$CLI/runtimes/x86_64/bin/python3.12" -V 2>&1)"
    printf 'Architectures: x86_64 (macOS 10.15+), arm64 (macOS 11+)\n'
} > "$CLI/README.txt"

echo "    bundled CLI + 2 runtimes: $(du -sh "$CLI" | cut -f1)"
