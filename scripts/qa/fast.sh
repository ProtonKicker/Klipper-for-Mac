#!/bin/sh
# ---------------------------------------------------------------------------
# scripts/qa/fast.sh -- Tier 1 of the Klipper-for-Mac quality gate.
#
# Goal: everything a human should run a hundred times a day, in <20 seconds:
#   sh      bash -n (syntax) on every shell script in the repo
#   swift   swiftc -parse on every Swift source + direct swiftc logic checks
#   py      ruff check klipperformac           (config: pyproject.toml)
#   compile python -m compileall klipperformac (catches syntax errors ruff can)
#   test    the fast offline subset: tests/test_*.py + tests/regress,
#           -m "not live and not hardware"
#
# DESIGN NOTES
#  * POSIX sh on purpose. macOS ships bash 3.2 and GNU make 3.81; CI runs this
#    on Linux too. No arrays, no [[ ]], no bashisms.
#  * It does NOT stop at the first failure. Every enabled step runs, then all
#    failures are reported together (rust-compiler style: see everything you
#    broke in one pass).
#  * Steps are individually selectable: QA_ONLY=py,swift scripts/qa/fast.sh
#    (used by `make lint-py`, `lint-sh`, `lint-swift`).
#  * scripts/qa/full.sh calls this script first, so tier 2 can never drift
#    away from tier 1. CI (.github/workflows/ci.yml) calls full.sh. One source
#    of truth.
#
# ENV OVERRIDES
#   PYTHON=<path>          interpreter for lint/pytest (default: .venv-test/bin/python
#                          if it exists, else python3)
#   QA_ONLY=<list>         comma-separated subset of: sh,swift,py,compile,test
#   QA_SKIP_RUFF=1         skip the ruff step instead of failing when ruff is absent
#   QA_SKIP_SWIFT=1        skip the swift step instead of failing when swiftc is absent
#   QA_FAST_MAX_SECONDS=N  hard-fail if the pytest step exceeds N seconds (0 = warn only, default)
#   RUFF=<cmd>             ruff invocation override
#   PYTEST_EXTRA=<args>    extra pytest arguments (e.g. "-k foo", "-p no:randomly")
# ---------------------------------------------------------------------------
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT" || exit 1

# ---------------------------------------------------------------- helpers --
STEP_FAIL=""
STEP_SKIP=""

qa_enabled() {
    # $1 = step name. All steps enabled unless QA_ONLY names a subset.
    [ -z "${QA_ONLY:-}" ] && return 0
    case ",$(printf '%s' "$QA_ONLY" | tr -d ' \t')," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

qa_failed() { [ -n "$STEP_FAIL" ]; }

qa_note_fail() { STEP_FAIL="$STEP_FAIL $1"; }
qa_note_skip() { STEP_SKIP="$STEP_SKIP $1"; printf '  [skip] %s\n' "$1"; }

qa_header() { printf '\n==> %s\n' "$1"; }

# ------------------------------------------------------------ interpreter --
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x .venv-test/bin/python ]; then
    PY=.venv-test/bin/python
else
    PY=$(command -v python3 || command -v python)
fi
if [ -z "${PY:-}" ] || ! "$PY" -c 'import sys' >/dev/null 2>&1; then
    printf 'ERROR: no usable Python interpreter (%s).\n' "${PY:-<empty>}"
    printf '       Set PYTHON=/path/to/python  (see QA.md).\n'
    exit 2
fi

RUFF_CMD="${RUFF:-}"
if [ -z "$RUFF_CMD" ]; then
    if "$PY" -m ruff --version >/dev/null 2>&1; then
        RUFF_CMD="$PY -m ruff"
    elif command -v ruff >/dev/null 2>&1; then
        RUFF_CMD=ruff
    fi
fi

# ------------------------------------------------- shell script discovery --
# Prune anything that is not ours to lint: .git, virtualenvs, the pristine
# upstream checkouts (AGENTS.md rule 1), build output, vendored asset dirs.
# The expression is repeated in both finds below; keep the two in sync.
PRUNE_NAME='\( -name .git -o -name .venv -o -name .venv-test -o -name venv \
-o -name upstream -o -name node_modules -o -name .build -o -name dist \
-o -name __pycache__ -o -name .hypothesis -o -name .pytest_cache \
-o -name screenshots -o -name "ui inspirations" \)'

list_sh() {
    # Shell scripts by extension, plus extension-less executables that carry a
    # shell shebang (bin/klipperformac is #!/bin/sh with no extension).
    # shellcheck disable=SC2086  # $PRUNE_NAME is a find expression, not paths
    eval "find . $PRUNE_NAME -prune -o -type f \
        \\( -name '*.sh' -o -name '*.bash' -o -name '*.ksh' -o -name '*.zsh' \\) -print"
    eval "find . $PRUNE_NAME -prune -o -type f -perm -u+x ! -name '*.*' -print" |
        while IFS= read -r f; do
            case "$(sed -n '1p' "$f" 2>/dev/null || :)" in
                \#!*sh*) printf '%s\n' "$f" ;;
            esac
        done
}

# Introspection hook for tier 2 (scripts/qa/full.sh shellcheck step) so the
# discovery rules exist exactly once, here. Prints one path per line and stops.
if [ "${1:-}" = "--list-shell" ]; then
    list_sh | sort -u
    exit 0
fi

# =============================================================== step: sh ===
if qa_enabled sh; then
    qa_header "shell: bash -n (syntax) on every shell script"
    n=0
    bad=""
    # while-read (not for-in-$( )) so paths with spaces survive word splitting.
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        n=$((n + 1))
        if ! bash -n "$f" 2>/tmp/qa_bashn.err; then
            printf '  FAIL bash -n: %s\n' "$f"
            sed -e 's/^/      /' /tmp/qa_bashn.err
            bad="$bad $f"
        fi
    done <<EOF
$(list_sh | sort -u)
EOF
    if [ "$n" -eq 0 ]; then
        printf '  WARN  no shell scripts found (did the pruning change?)\n'
    else
        printf '  %s file(s) syntax-checked\n' "$n"
    fi
    [ -n "$bad" ] && qa_note_fail "sh:$bad"
    [ -n "$bad" ] || printf '  OK\n'
fi

# ============================================================ step: swift ===
if qa_enabled swift; then
    qa_header "swift: swiftc -parse on sources + direct swift logic checks"
    if ! command -v swiftc >/dev/null 2>&1; then
        if [ "${QA_SKIP_SWIFT:-0}" = 1 ]; then
            qa_note_skip "swift: swiftc not installed"
        else
            printf '  FAIL no swiftc found. Install Xcode Command Line Tools:\n'
            printf '         xcode-select --install\n'
            printf '       (set QA_SKIP_SWIFT=1 to make this a skip instead)\n'
            qa_note_fail swift
        fi
    else
        swift_srcs=$(find KlipperMac/Sources -name '*.swift' 2>/dev/null | sort)
        if [ -z "$swift_srcs" ]; then
            printf '  WARN no Swift sources under KlipperMac/Sources\n'
        else
            cnt=$(printf '%s\n' "$swift_srcs" | wc -l | tr -d ' ')
            # -parse = lex+parse only: no SDK, no codegen, no signing. ~1s.
            if printf '%s\n' "$swift_srcs" | xargs swiftc -parse >/tmp/qa_swift.err 2>&1; then
                printf '  OK %s Swift file(s) parse\n' "$cnt"
                if sh scripts/qa/swift_logic.sh >/tmp/qa_swift_logic.out 2>/tmp/qa_swift_logic.err; then
                    printf '  OK direct swift logic checks\n'
                else
                    printf '  FAIL direct swift logic checks\n'
                    sed -e 's/^/      /' /tmp/qa_swift_logic.err | tail -30
                    sed -e 's/^/      /' /tmp/qa_swift_logic.out | tail -30
                    qa_note_fail swift-logic
                fi
            else
                printf '  FAIL swiftc -parse (%s files)\n' "$cnt"
                sed -e 's/^/      /' /tmp/qa_swift.err | tail -30
                qa_note_fail swift
            fi
        fi
    fi
fi

# ============================================================== step: py ===
if qa_enabled py; then
    qa_header "python: ruff check klipperformac (E,F,B9,W; baseline in pyproject.toml)"
    if [ -z "$RUFF_CMD" ]; then
        if [ "${QA_SKIP_RUFF:-0}" = 1 ]; then
            qa_note_skip "py: ruff not installed (QA_SKIP_RUFF=1)"
        else
            printf '  FAIL ruff is not installed for %s\n' "$PY"
            printf '         %s -m pip install -r requirements-dev.txt\n' "$PY"
            printf '       (set QA_SKIP_RUFF=1 to make this a skip instead)\n'
            qa_note_fail ruff-missing
        fi
    else
        # shellcheck disable=SC2086
        if $RUFF_CMD check klipperformac; then
            printf '  OK ruff clean (no new violations)\n'
        else
            qa_note_fail ruff
        fi
    fi
fi

# ========================================================== step: compile ===
if qa_enabled compile; then
    qa_header "python: compileall klipperformac (byte-code compile)"
    if "$PY" -m compileall -q klipperformac; then
        printf '  OK all modules compile\n'
    else
        qa_note_fail compileall
    fi
fi

# ============================================================= step: test ===
if qa_enabled test; then
    qa_header "tests: fast offline subset (tests/test_*.py + tests/regress)"
    if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
        printf '  FAIL pytest is not installed for %s\n' "$PY"
        printf '         %s -m pip install -r requirements-dev.txt\n' "$PY"
        qa_note_fail pytest-missing
    else
        fast_sel=""
        for f in tests/test_*.py; do
            [ -f "$f" ] && fast_sel="$fast_sel $f"
        done
        [ -d tests/regress ] && fast_sel="$fast_sel tests/regress"
        if [ -z "$fast_sel" ]; then
            printf '  FAIL no fast-tier test files found (tests/test_*.py, tests/regress)\n'
            qa_note_fail pytest-nofiles
        else
            t0=$(date +%s)
            # -x: a fast hook must stop on the first failure, not grind for minutes.
            # shellcheck disable=SC2086
            PYTHONPATH=. "$PY" -m pytest $fast_sel -q -x \
                -m "not live and not hardware" ${PYTEST_EXTRA:-}
            rc=$?
            t1=$(date +%s)
            dur=$((t1 - t0))
            budget=${QA_FAST_MAX_SECONDS:-0}
            printf '  pytest step: %ss (budget for this tier: 20s)\n' "$dur"
            if [ "$rc" -ne 0 ]; then
                qa_note_fail "pytest(rc=$rc)"
            elif [ "$budget" -gt 0 ] && [ "$dur" -gt "$budget" ]; then
                printf '  FAIL fast tier took %ss > QA_FAST_MAX_SECONDS=%s\n' "$dur" "$budget"
                qa_note_fail "pytest-slow($dur s)"
            elif [ "$dur" -gt 20 ]; then
                printf '  WARN fast tier exceeded its 20s budget.\n'
                printf '       Usually a newly heavy test in tests/regress: profile it\n'
                printf '       with --durations=10, then @pytest.mark.slow it or move it\n'
                printf '       to tests/deep/ (see QA.md "Adding tests").\n'
            fi
        fi
    fi
fi

# ============================================================== summary ====
printf '\n--------------------------------------------------------------\n'
if qa_failed; then
    printf 'QA-FAST: FAIL ->%s\n' "$STEP_FAIL"
    printf '  Fix the above. Escape hatch (last resort, must be justified in the\n'
    printf '  commit message): SKIP_QA=1 git commit ...  -- see QA.md.\n'
    exit 1
fi
if [ -n "$STEP_SKIP" ]; then
    printf 'QA-FAST: PASS (skipped:%s)\n' "$STEP_SKIP"
else
    printf 'QA-FAST: PASS\n'
fi
exit 0
