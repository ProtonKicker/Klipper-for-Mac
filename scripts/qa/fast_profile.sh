#!/bin/sh
# Profile the fast offline pytest subset and print the slowest tests.
#
# ENV:
#   QA_FAST_PROFILE_COUNT   how many slow tests to print (default: 15)
#   QA_FAST_PROFILE_MIN     minimum duration to show in seconds (default: 0.05)

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT" || exit 1

COUNT=${QA_FAST_PROFILE_COUNT:-15}
MIN=${QA_FAST_PROFILE_MIN:-0.05}

exec sh scripts/qa/fast_pytest.sh \
    --durations="$COUNT" \
    --durations-min="$MIN"
