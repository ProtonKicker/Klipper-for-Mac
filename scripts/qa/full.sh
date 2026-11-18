#!/bin/sh
# Full QA gate (pre-push): everything fast.sh does, plus the universal GUI
# build at the macOS 10.15 floor and a CLI import/smoke check.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

sh scripts/qa/fast.sh

echo "==> CLI import + data smoke"
PYTHONPATH=. python3 -c "from klipperformac import cli, paths, versions; print('imports ok')"
PYTHONPATH=. python3 -m klipperformac data >/dev/null
echo "    cli data ok"

if command -v xcrun >/dev/null 2>&1; then
    echo "==> GUI universal build (macOS 10.15 floor)"
    KlipperMac/build-app.sh >/dev/null
    echo "    gui build ok"
else
    echo "==> xcrun not found; skipping GUI build (CLI checks passed)"
fi

echo "qa-full passed"