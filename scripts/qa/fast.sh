#!/bin/sh
# Fast offline QA gate (pre-commit). Lint + format + offline pytest.
# Skips the GUI build; `scripts/qa/full.sh` (pre-push) adds that.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

if [ -x .venv-test/bin/ruff ]; then
    RUFF=.venv-test/bin/ruff
    PYTEST=.venv-test/bin/pytest
else
    RUFF=${RUFF:-ruff}
    PYTEST=${PYTEST:-"python3 -m pytest"}
fi

echo "==> ruff check"
"$RUFF" check klipperformac bin scripts tests
echo "==> ruff format --check"
"$RUFF" format --check klipperformac bin scripts tests --quiet
echo "==> offline pytest"
$PYTEST -q

echo "qa-fast passed"