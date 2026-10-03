#!/bin/sh
# curl-friendly one-liner installer for Klipper for Mac (CLI + TUI, no GUI bundle yet).
#   curl -fsSL https://raw.githubusercontent.com/<owner>/MaKlipper/main/scripts/install.sh | sh
set -e

REPO="${KLIPPERFORMAC_REPO:-}"
SRC_DIR="${KLIPPERFORMAC_SRC:-$HOME/.klipperformac/src/MaKlipper}"
BIN_DIR="${KLIPPERFORMAC_BIN_DIR:-$HOME/.local/bin}"

say() { printf 'klipperformac-install: %s\n' "$1"; }

# Repo source: explicit env, or the repo this script was run from (git
# checkout), or the configured origin — refuse to guess placeholders.
if [ -z "$REPO" ]; then
  for cand in "$PWD" "$PWD/../.."; do
    if [ -d "$cand/.git" ]; then
      REPO=$(git -C "$cand" remote get-url origin 2>/dev/null || true)
      [ -n "$REPO" ] && break
    fi
  done
fi
if [ -z "$REPO" ] || echo "$REPO" | grep -q "YOURHANDLE\|<owner>"; then
  say "ERROR: set KLIPPERFORMAC_REPO=https://github.com/<owner>/MaKlipper (placeholder repo URL)."
  exit 1
fi

# Prechecks.
command -v git  >/dev/null 2>&1 || { say "git not found. Run: xcode-select --install"; exit 1; }
command -v cc   >/dev/null 2>&1 || { say "compiler not found. Run: xcode-select --install"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null \
  || { say "python3 >= 3.8 required for the CLI itself"; exit 1; }

# Fetch or update the app sources.
mkdir -p "$(dirname "$SRC_DIR")"
if [ -d "$SRC_DIR/.git" ]; then
  say "updating existing checkout"
  if ! git -C "$SRC_DIR" pull --ff-only --quiet; then
    say "WARNING: checkout at $SRC_DIR is dirty/diverged; leaving as-is."
  fi
else
  say "cloning $REPO"
  git clone --quiet "$REPO" "$SRC_DIR"
fi

# Install the launcher.
mkdir -p "$BIN_DIR"
ln -sf "$SRC_DIR/bin/klipperformac" "$BIN_DIR/klipperformac"
say "installed klipperformac -> $BIN_DIR/klipperformac"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "NOTE: add $BIN_DIR to your PATH, e.g. in ~/.zshrc:"
     say "  export PATH=\$HOME/.local/bin:\$PATH" ;;
esac

say "next step: klipperformac setup"
