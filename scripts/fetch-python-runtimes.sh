#!/bin/sh
# Fetch relocatable CPython runtimes for the bundled CLI into .build/pythons.
# Uses `uv python install` with --install-dir so nothing outside this checkout
# is changed. Both runtimes match the GUI's OS floors (Intel 10.15 / ARM 11.0).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST=${KLIPPERFORMAC_RUNTIME_DIR:-"$ROOT/.build/pythons"}

if ! command -v uv >/dev/null 2>&1; then
    echo "fetch-python-runtimes: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

mkdir -p "$DEST"
uv python install --install-dir "$DEST" --no-bin --no-cache --no-progress \
    cpython-3.12-macos-x86_64-none cpython-3.12-macos-aarch64-none

# Stable names used by bundle-cli.sh (uv also creates versioned directories).
for target in cpython-3.12-macos-x86_64-none cpython-3.12-macos-aarch64-none; do
    [ -x "$DEST/$target/bin/python3.12" ] || {
        echo "fetch-python-runtimes: $target missing after install" >&2
        exit 1
    }
done
echo "runtimes ready in $DEST"
