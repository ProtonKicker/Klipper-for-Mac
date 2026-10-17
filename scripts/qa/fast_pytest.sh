#!/bin/sh
# Run the fast offline pytest subset used by qa-fast:
#   tests/test_*.py + tests/regress
# with the standard "not live and not hardware" marker gate.
#
# Extra pytest arguments may be passed on argv; callers can use that for
# profiling flags like --durations without re-encoding the fast-tier selector.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT" || exit 1

if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x .venv-test/bin/python ]; then
    PY=.venv-test/bin/python
else
    PY=$(command -v python3 || command -v python)
fi

if [ -z "${PY:-}" ] || ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
    printf 'ERROR: pytest is not available for %s\n' "${PY:-<empty>}"
    printf '       Install dev dependencies, e.g. %s -m pip install -r requirements-dev.txt\n' "${PY:-python3}"
    exit 2
fi

fast_sel=""
for f in tests/test_*.py; do
    [ -f "$f" ] && fast_sel="$fast_sel $f"
done
[ -d tests/regress ] && fast_sel="$fast_sel tests/regress"

if [ -z "$fast_sel" ]; then
    printf 'ERROR: no fast-tier test files found (tests/test_*.py, tests/regress)\n'
    exit 2
fi

# shellcheck disable=SC2086
exec env PYTHONPATH=. "$PY" -m pytest $fast_sel -q -x \
    -m "not live and not hardware" "$@"
