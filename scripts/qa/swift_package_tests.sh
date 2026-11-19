#!/bin/sh
# Run the real Swift package tests when the local Apple toolchain can support
# them. On CLT-only Macs where `swift test` is known to be blocked, print the
# exact reason and exit 3 so callers can treat that as a skip/advisory state.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PKG="$ROOT/KlipperMac"

have_xcode_app() {
    find /Applications -maxdepth 1 -type d -name 'Xcode*.app' | grep . >/dev/null 2>&1
}

note_blocked() {
    printf '%s\n' "$1"
    exit 3
}

if ! command -v swift >/dev/null 2>&1; then
    note_blocked "swift test blocked: no \`swift\` executable is available on this Mac."
fi

if ! command -v xcrun >/dev/null 2>&1; then
    note_blocked "swift test blocked: no \`xcrun\` executable is available on this Mac."
fi

devdir=$(xcode-select -p 2>/dev/null || printf '<unknown>')
if ! platform_path=$(xcrun --sdk macosx --show-sdk-platform-path 2>/tmp/qa_swiftpkg_xcrun.err); then
    printf 'swift test blocked: xcrun cannot resolve the macOS platform path.\n'
    printf '  active developer dir: %s\n' "$devdir"
    if have_xcode_app; then
        printf '  Xcode app found under /Applications, but xcode-select still points at a broken toolchain.\n'
        printf '  stderr:\n'
    else
        printf '  no Xcode*.app found under /Applications; this Mac appears to have Command Line Tools only.\n'
        printf '  stderr:\n'
    fi
    sed -e 's/^/    /' /tmp/qa_swiftpkg_xcrun.err
    exit 3
fi

printf 'swift test toolchain ready:\n'
printf '  developer dir: %s\n' "$devdir"
printf '  platform path: %s\n' "$platform_path"

exec swift test --package-path "$PKG"
