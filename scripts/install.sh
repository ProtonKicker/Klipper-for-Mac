#!/bin/sh
# curl-friendly one-liner installer for Klipper for Mac (CLI + TUI, no GUI bundle yet).
#   curl -fsSL https://raw.githubusercontent.com/ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh
set -e

REPO="${KLIPPERFORMAC_REPO:-https://github.com/ProtonKicker/Klipper-for-Mac.git}"
SRC_DIR="${KLIPPERFORMAC_SRC:-$HOME/.klipperformac/src/Klipper-for-Mac}"
BIN_DIR="${KLIPPERFORMAC_BIN_DIR:-$HOME/.local/bin}"

say() { printf 'klipperformac-install: %s\n' "$1"; }

# Repo source: explicit env wins, else a local Klipper-for-Mac checkout's
# origin, else the official default above.
if [ -n "${KLIPPERFORMAC_REPO:-}" ]; then
  REPO="$KLIPPERFORMAC_REPO"
else
  for cand in "$PWD" "$PWD/.."; do
    if [ -d "$cand/.git" ] && [ -f "$cand/klipperformac/__main__.py" ]; then
      LOCAL_REPO=$(git -C "$cand" remote get-url origin 2>/dev/null || true)
      [ -n "$LOCAL_REPO" ] && REPO="$LOCAL_REPO" && break
    fi
  done
fi
if echo "$REPO" | grep -q "YOURHANDLE\|<owner>"; then
  say "ERROR: set KLIPPERFORMAC_REPO=https://github.com/<owner>/Klipper-for-Mac.git"
  exit 1
fi

# Prechecks.
command -v git  >/dev/null 2>&1 || { say "git not found. Run: xcode-select --install"; exit 1; }
command -v gcc   >/dev/null 2>&1 || { say "compiler not found. Run: xcode-select --install"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null \
  || { say "python3 >= 3.10 required (setup needs it too). Try: brew install python@3.12"; exit 1; }

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

# Offer to run setup immediately (curl | sh has no TTY on stdin, so ask via
# /dev/tty; skip when the user is clearly unattended).
KLP="$SRC_DIR/bin/klipperformac"
if [ -z "${KLIPPERFORMAC_SKIP_SETUP:-}" ] && [ -x "$KLP" ] \
   && { [ -t 0 ] || { [ -c /dev/tty ] && ( : <>/dev/tty ) 2>/dev/null; }; }; then
  printf 'klipperformac-install: run setup now (fetch + build, a few minutes)? [Y/n] ' > /dev/tty
  read -r ANSWER < /dev/tty || ANSWER=y
  case "$ANSWER" in
    [nN]*) say "next step: klipperformac setup" ;;
    *) say "starting setup"
       if [ -t 1 ]; then
         "$KLP" setup
       else
         "$KLP" setup < /dev/null
       fi ;;
  esac
else
  say "next step: klipperformac setup"
fi
