#!/bin/sh
# curl-friendly one-liner installer for maklipper (CLI only, no GUI).
#   curl -fsSL https://raw.githubusercontent.com/<you>/MaKlipper/main/scripts/install.sh | sh
set -e

SRC_DIR="${MAKLIPPER_SRC:-$HOME/.maklipper/src/MaKlipper}"
BIN_DIR="${MAKLIPPER_BIN_DIR:-$HOME/.local/bin}"
REPO="${MAKLIPPER_REPO:-https://github.com/YOURHANDLE/MaKlipper}"

say() { printf 'maklipper-install: %s\n' "$1"; }

# Precheck: Xcode command line tools provide git/gcc.
if ! command -v git >/dev/null 2>&1; then
  say "git not found. Run: xcode-select --install"
  exit 1
fi
if ! cc --version >/dev/null 2>&1; then
  say "compiler not found. Run: xcode-select --install"
  exit 1
fi

# Fetch or update the app sources.
mkdir -p "$(dirname "$SRC_DIR")"
if [ -d "$SRC_DIR/.git" ]; then
  say "updating existing checkout"
  git -C "$SRC_DIR" pull --ff-only --quiet
else
  say "cloning MaKlipper"
  git clone --quiet "$REPO" "$SRC_DIR"
fi

# Install the launcher.
mkdir -p "$BIN_DIR"
ln -sf "$SRC_DIR/bin/maklipper" "$BIN_DIR/maklipper"
say "installed maklipper -> $BIN_DIR/maklipper"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "NOTE: add $BIN_DIR to your PATH, e.g. in ~/.zshrc:"
     say "  export PATH=\$HOME/.local/bin:\$PATH" ;;
esac

say "next step: maklipper setup"
