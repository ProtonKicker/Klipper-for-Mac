#!/bin/sh
# ---------------------------------------------------------------------------
# scripts/qa/full.sh -- Tier 2 of the Klipper-for-Mac quality gate.
#
#   1. delegates to tier 1 in full (scripts/qa/fast.sh: sh/swift/ruff/compile/tests)
#   2. the real Swift package tests, when the local Apple toolchain can run them
#      (advisory/skip on CLT-only Macs; required with QA_REQUIRE_SWIFTPM=1)
#   3. the ENTIRE offline suite: tests/, -m "not live and not hardware"
#      (~3 min on a Mac, no hardware attached, no writes to the running stack)
#   4. shellcheck on every shell script, when shellcheck is installed
#      (brew install shellcheck -- see QA.md; skipped with a notice if missing,
#       made mandatory with QA_REQUIRE_SHELLCHECK=1 -- CI sets that)
#
# This is what the pre-push hook and CI run. `make release-check` = this +
# doctor + build-gui.
#
# Shell discovery for step 3 is delegated back to fast.sh (`--list-shell`) so
# the prune rules exist in exactly one place.
#
# ENV OVERRIDES  (everything fast.sh accepts is passed through untouched)
#   PYTHON=<path>                 interpreter for the suite
#   QA_ONLY=swiftpkg,suite,shellcheck
#                                 skip tier 1 and run only the named tier-2 steps
#                                 (`make test-offline` uses QA_ONLY=suite)
#   QA_REQUIRE_SWIFTPM=1          fail instead of skipping when `swift test`
#                                 is blocked by the local Apple toolchain state
#   QA_REQUIRE_SHELLCHECK=1       fail instead of skipping when shellcheck is absent
#   QA_SHELLCHECK_FAILS_GATE=1    a shellcheck finding fails the tier (default: advisory)
#   SHELLCHECK=<cmd>              shellcheck invocation override
#   PYTEST_EXTRA=<args>           extra pytest arguments
# ---------------------------------------------------------------------------
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT" || exit 1

STEP_FAIL=""
qa_note_fail() { STEP_FAIL="$STEP_FAIL $1"; }

qa_enabled() {
    # Mirrors fast.sh: all steps on unless QA_ONLY names a subset.
    [ -z "${QA_ONLY:-}" ] && return 0
    case ",$(printf '%s' "$QA_ONLY" | tr -d ' \t')," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

# ------------------------------------------------------------ interpreter --
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x .venv-test/bin/python ]; then
    PY=.venv-test/bin/python
else
    PY=$(command -v python3 || command -v python)
fi

# ================================================================ tier 1 ====
# Delegation (not duplication) is what keeps the tiers from drifting apart.
# Ask for tier-2 work only with QA_ONLY=suite / shellcheck / suite,shellcheck.
run_tier1=1
qa_enabled fast || run_tier1=0
if [ "$run_tier1" -eq 1 ]; then
    printf '\n### TIER 1 (scripts/qa/fast.sh)\n'
    sh scripts/qa/fast.sh || qa_note_fail tier1
fi

# ============================================================= step: suite ==
if qa_enabled swiftpkg; then
    printf '\n### TIER 2 -- swift package tests\n'
    if sh scripts/qa/swift_package_tests.sh; then
        printf '  swift package tests: PASS\n'
    else
        rc=$?
        if [ "$rc" -eq 3 ]; then
            if [ "${QA_REQUIRE_SWIFTPM:-0}" = 1 ]; then
                printf '  FAIL swift package tests are required here but the toolchain is blocked.\n'
                qa_note_fail swiftpkg-blocked
            else
                printf '  [skip] swift package tests blocked on this Mac -- advisory tier, not a failure.\n'
                printf '         Make it mandatory here: QA_REQUIRE_SWIFTPM=1 scripts/qa/full.sh\n'
            fi
        else
            qa_note_fail "swiftpkg(rc=$rc)"
        fi
    fi
fi

# ============================================================= step: suite ==
if qa_enabled suite; then
    printf '\n### TIER 2 -- offline suite (the whole tests/ tree)\n'
    if [ -z "${PY:-}" ] || ! "$PY" -m pytest --version >/dev/null 2>&1; then
        printf '  FAIL pytest is not available (%s)\n' "${PY:-<none>}"
        printf '         %s -m pip install -r requirements-dev.txt\n' "${PY:-python3}"
        qa_note_fail pytest-missing
    else
        t0=$(date +%s)
        # Deliberately NOT -x: at ~3 minutes, seeing every failure in one pass is
        # worth more than stopping early. -ra reprints the failure list at the end.
        # shellcheck disable=SC2086
        PYTHONPATH=. "$PY" -m pytest tests -q -ra \
            -m "not live and not hardware" ${PYTEST_EXTRA:-}
        rc=$?
        t1=$(date +%s)
        printf '  offline suite: %ss\n' "$((t1 - t0))"
        [ "$rc" -eq 0 ] || qa_note_fail "suite(rc=$rc)"
    fi
fi

# ====================================================== step: shellcheck ====
if qa_enabled shellcheck; then
    printf '\n### TIER 2 -- shellcheck\n'
    SC="${SHELLCHECK:-}"
    if [ -z "$SC" ] && command -v shellcheck >/dev/null 2>&1; then
        SC=$(command -v shellcheck)
    fi
    if [ -z "$SC" ]; then
        if [ "${QA_REQUIRE_SHELLCHECK:-0}" = 1 ]; then
            printf '  FAIL shellcheck is required here but not installed.\n'
            printf '         macOS:  brew install shellcheck\n'
            printf '         Linux:  apt-get install -y shellcheck\n'
            qa_note_fail shellcheck-missing
        else
            printf '  [skip] shellcheck not installed -- advisory tier, not a failure.\n'
            printf '         Install it so shell actually gets linted:\n'
            printf '             brew install shellcheck      (Homebrew formula: shellcheck)\n'
            printf '         Or force the issue: QA_REQUIRE_SHELLCHECK=1 scripts/qa/full.sh\n'
        fi
    else
        files=$(sh scripts/qa/fast.sh --list-shell)
        if [ -z "$files" ]; then
            printf '  [skip] no shell scripts discovered\n'
        else
            n=0
            bad=""
            while IFS= read -r f; do
                [ -n "$f" ] || continue
                n=$((n + 1))
                # shellcheck disable=SC2086
                if ! "$SC" "$f" >/tmp/qa_shellcheck.err 2>&1; then
                    printf '  FINDINGS shellcheck: %s\n' "$f"
                    sed -e 's/^/      /' /tmp/qa_shellcheck.err | head -60
                    bad="$bad $f"
                fi
            done <<EOF
$files
EOF
            printf '  %s file(s) shellchecked (%s)\n' "$n" "$SC"
            if [ -n "$bad" ]; then
                if [ "${QA_SHELLCHECK_FAILS_GATE:-0}" = 1 ]; then
                    qa_note_fail "shellcheck:$bad"
                else
                    printf '  NOTE shellcheck findings are advisory by default.\n'
                    printf '       Make them block: QA_SHELLCHECK_FAILS_GATE=1\n'
                fi
            else
                printf '  OK shellcheck clean\n'
            fi
        fi
    fi
fi

# ============================================================== summary ====
printf '\n--------------------------------------------------------------\n'
if [ -n "$STEP_FAIL" ]; then
    printf 'QA-FULL: FAIL ->%s\n' "$STEP_FAIL"
    exit 1
fi
printf 'QA-FULL: PASS\n'
exit 0
