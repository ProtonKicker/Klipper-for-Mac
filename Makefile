# ---------------------------------------------------------------------------
# Klipper-for-Mac -- the quality gate. One entry point for every check.
#
#   make              -> qa-fast   (~10 s: syntax + lint + fast tests)
#   make qa-full      -> offline suite + optional SwiftPM probe (~3-5 min)
#   make help         -> target list
#
# Portability rules this file obeys, so it behaves the same on a fresh Mac, on
# this Mac with the live stack running, and on Linux CI:
#   * no GNU-make-4-only features (macOS ships make 3.81 -- including
#     .DEFAULT_GOAL, so qa-fast is simply the FIRST target in this file)
#   * interpreter = .venv-test/bin/python when present, else python3
#   * every tool/skip is overridable from the environment, never hardcoded
#   * all real logic lives in scripts/qa/*.sh; targets here are thin wrappers,
#     so `make`, the git hooks and CI can never disagree about a check
#
# ENV OVERRIDES
#   PYTHON=<path>               interpreter for lint+test (default: .venv-test/bin/python, else python3)
#   KLIPPERFORMAC_PYTHON=<path>  interpreter for the *CLI* targets (doctor);
#                               default is bin/klipperformac's own python3, i.e.
#                               exactly how the user runs the tool
#   RUFF / SHELLCHECK / PYTEST_EXTRA / QA_ONLY / QA_SKIP_RUFF / QA_SKIP_SWIFT
#                               passed through to scripts/qa/*.sh -- see each
#                               script header for the full list
#   KLIPPERFORMAC_ALLOW_LIVE=1  required for `make test-live` (touches the real stack)
#   SKIP_QA=1                   honoured by the git hooks, not by make
# ---------------------------------------------------------------------------

SHELL := /bin/sh

# Prefer the caller-selected interpreter, then the offline test venv, then the
# system interpreter.
PY := $(if $(PYTHON),$(PYTHON),$(shell if [ -x .venv-test/bin/python ]; then echo .venv-test/bin/python; else command -v python3 || echo python; fi))
QA := scripts/qa
GITDIR := $(shell git rev-parse --git-dir 2>/dev/null)

# ============================================================ TIER 1 ========
# First target in the file = the default goal on make 3.81 and 4.x alike.
.PHONY: qa-fast
qa-fast:
	@sh $(QA)/fast.sh

# ============================================================ TIER 2 ========
.PHONY: qa-full
qa-full:
	@sh $(QA)/full.sh

# ---------------------------------------------------------- single steps ----
# Routed through fast.sh so CI and make can never disagree about what a lint is.
.PHONY: lint-py
lint-py:
	@QA_ONLY=py sh $(QA)/fast.sh

.PHONY: lint-sh
lint-sh:
	@QA_ONLY=sh sh $(QA)/fast.sh

.PHONY: lint-swift
lint-swift:
	@QA_ONLY=swift sh $(QA)/fast.sh

.PHONY: test-offline
test-offline:
	@QA_ONLY=suite sh $(QA)/full.sh

.PHONY: test-swiftpkg
test-swiftpkg:
        @QA_ONLY=swiftpkg sh $(QA)/full.sh

# --------------------------------------------------------------- test-live --
# Guarded: these tests talk to the user's ACTUAL stack and/or a board on USB.
# AGENTS.md forbids touching a running stack unasked, so a bare `make test-live`
# refuses; the opt-in env var has to be said out loud.
.PHONY: test-live
test-live:
	@if [ "$${KLIPPERFORMAC_ALLOW_LIVE:-0}" != "1" ]; then \
	  printf '%s\n' \
	    'REFUSED: test-live runs against your RUNNING stack and possibly a USB board.' \
	    '' \
	    '  It can start/stop klipper+moonraker, reset the MCU and write real logs.' \
	    '  Nothing here does that by accident -- that is the project premise.' \
	    '' \
	    '  To proceed deliberately:' \
	    '      KLIPPERFORMAC_ALLOW_LIVE=1 make test-live' \
	    '' \
	    '  It runs: pytest tests -q -ra -m "live or hardware"' \
	    '  If that collects 0 tests, nothing is tagged @pytest.mark.live /' \
	    '  @pytest.mark.hardware yet -- see QA.md, "Live and hardware tiers".'; \
	  exit 2; \
	fi; \
	printf 'KLIPPERFORMAC_ALLOW_LIVE=1 given: running the live/hardware tier.\n'; \
	PYTHONPATH=. $(PY) -m pytest tests -q -ra -m "live or hardware" $(PYTEST_EXTRA)

# ---------------------------------------------------------------- doctor ----
# `klipperformac doctor` is read-only (git status on the upstream checkouts, a
# scratch C-helper build, pmset/state reads) and it is the check AGENTS.md rule
# 4 demands. It runs the CLI the way the user does, so it uses bin/klipperformac
# 's own interpreter unless KLIPPERFORMAC_PYTHON is set.
.PHONY: doctor
doctor:
	@./bin/klipperformac doctor

# ------------------------------------------------------------- build-gui ----
.PHONY: build-gui
build-gui:
	@cd KlipperMac && ./build-app.sh

# --------------------------------------------------------- install-hooks ----
# Deliberately does NOT touch git config: no core.hooksPath, no `git config`
# write at all. It copies the tracked hook sources into $(GITDIR)/hooks/ and
# chmod +x's them. Running it is the only way hooks become active, and it is
# safe to re-run (it backs up a differing pre-existing hook first).
.PHONY: install-hooks
install-hooks:
	@if [ -z "$(GITDIR)" ]; then \
	  echo 'ERROR: not inside a git repository, cannot install hooks.'; exit 1; \
	fi; \
	if [ ! -d scripts/githooks ]; then \
	  echo 'ERROR: scripts/githooks/ is missing.'; exit 1; \
	fi; \
	hp=$$(git config --get core.hooksPath 2>/dev/null || :); \
	if [ -n "$$hp" ]; then \
	  printf 'WARNING: core.hooksPath is set to "%s" -- git reads hooks from there\n' "$$hp"; \
	  printf '         instead of .git/hooks, so these hooks will NOT run. This\n'; \
	  printf '         target never changes that setting; it is your config, not ours.\n'; \
	fi; \
	mkdir -p "$(GITDIR)/hooks"; \
	for src in scripts/githooks/*; do \
	  name=$$(basename "$$src"); \
	  dst="$(GITDIR)/hooks/$$name"; \
	  if [ -f "$$dst" ] && ! cmp -s "$$src" "$$dst"; then \
	    cp "$$dst" "$$dst.pre-qa-backup"; \
	    printf '  backed up your existing %s -> %s.pre-qa-backup\n' "$$name" "$$name"; \
	  fi; \
	  cp "$$src" "$$dst"; \
	  chmod +x "$$dst"; \
	  printf '  installed %s\n' "$$dst"; \
	done; \
	printf '\nHooks are live in this worktree. Escape hatch: SKIP_QA=1 git commit ...\n'

# ---------------------------------------------------------------- release ---
# The gate before a tag: every check that can run without a board, then the two
# things that prove the product builds and this machine is sane. Prerequisite
# order matters: qa-full first (cheap, catches regressions), then doctor, then
# the slow universal .app build.
.PHONY: release-check
release-check: qa-full doctor build-gui
	@printf '\nRELEASE-CHECK: PASS (qa-full + doctor + build-gui)\n'

# --------------------------------------------------------- tooling/meta -----
.PHONY: qa-deps
qa-deps:
	$(PY) -m pip install -r requirements-dev.txt

# Prints the ruff debt still quarantined in pyproject.toml under
# [tool.ruff.lint.per-file-ignores]. --isolated bypasses the quarantine, so the
# real number is visible. The baseline may only SHRINK: when a file drops off
# this list, delete its line from pyproject.toml.
.PHONY: lint-py-baseline-report
lint-py-baseline-report:
	@printf 'Quarantined ruff baseline (counting through the per-file ignores):\n'
	@total=0; \
	for f in klipperformac/*.py; do \
	  n=$$($(PY) -m ruff check --isolated --select E,F,B9,W --line-length 100 \
	      --output-format concise "$$f" 2>/dev/null | grep ':' | wc -l | tr -d ' '); \
	  if [ "$$n" -gt 0 ]; then \
	    printf '  %-34s %s\n' "$$f" "$$n findings"; \
	  fi; \
	  total=$$((total + n)); \
	done; \
	printf '  TOTAL %s\n' "$$total"; \
	printf '0 = the baseline is empty; delete the per-file-ignores block from pyproject.toml.\n'

.PHONY: help
help:
	@printf '%s\n' \
	  'Klipper-for-Mac quality gate (interpreter: $(PY))' \
	  '' \
	  '  make                 qa-fast  -- tier 1: sh/swift syntax + ruff + compileall + fast tests (~10s)' \
          '  make qa-full         tier 2: qa-fast + Swift package tests when toolchain-ready + the offline suite + shellcheck' \
	  '  make lint-py         ruff check klipperformac only' \
	  '  make lint-sh         bash -n every shell script only' \
	  '  make lint-swift      swiftc -parse on KlipperMac/Sources only' \
	  '  make test-offline    the full offline pytest suite (~3min, no hardware)' \
          '  make test-swiftpkg   run/diagnose swift test for KlipperMac' \
	  '  make test-live       opt-in: tests marked live/hardware (needs the real stack)' \
	  '  make doctor          klipperformac doctor (env + pristine-upstream proof)' \
	  '  make build-gui       build the .app via KlipperMac/build-app.sh' \
	  '  make install-hooks   copy scripts/githooks/* into .git/hooks (+x)' \
	  '  make release-check   qa-full + doctor + build-gui' \
	  '  make qa-deps         pip install the dev tools for $(PY)' \
	  '  make lint-py-baseline-report   show the ruff debt still quarantined' \
	  '' \
	  'Docs: QA.md (tiers, markers, cost notes, adding tests).'
