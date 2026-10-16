# QA

Klipper for Mac has a two-tier local quality gate plus an opt-in live tier.

## Bootstrap

Create or refresh the offline test environment:

```sh
python3 -m venv .venv-test
.venv-test/bin/python -m pip install -U pip
make qa-deps
```

`make qa-deps` installs the Python-side dev tools from `requirements-dev.txt`.
Today that is intentionally small:

- `ruff`
- `pytest`
- `hypothesis`

Shell linting uses `shellcheck`, which is installed outside Python:

```sh
brew install shellcheck
```

## Tiers

- `make` or `make qa-fast`
  Fast inner-loop gate: shell syntax, Swift parse, `ruff`, `compileall`, and
  the fast offline subset (`tests/test_*.py` plus `tests/regress`).

- `make qa-full`
  Everything in `qa-fast`, then the entire offline test tree and `shellcheck`
  when available.

- `make test-live`
  Explicitly opt-in live/hardware tier only. This never runs by default.

- `make doctor`
  Read-only environment and upstream-pristine check.

- `make build-gui`
  Builds `KlipperMac/dist/Klipper for Mac.app`.

- `make release-check`
  `qa-full` + `doctor` + `build-gui`.

## Markers

Tests that touch the running stack or real hardware must declare that clearly:

- `@pytest.mark.live`
- `@pytest.mark.hardware`
- `@pytest.mark.slow`

Rules:

- a test that needs the local running stack must be marked `live`
- a test that needs USB serial, camera hardware, or macOS permissions must be
  marked `hardware`
- long-running tests should be marked `slow`

Right now the scaffold supports a live tier, but there are no dedicated
`live` / `hardware` tests checked in yet, so `make test-live` may collect zero
tests. That is fine; the scaffold is ready for them.

## Hooks

Install tracked hooks into `.git/hooks`:

```sh
make install-hooks
```

Installed behavior:

- `pre-commit` runs `scripts/qa/fast.sh`
- `pre-push` runs `scripts/qa/full.sh`

Escape hatch for emergencies only:

```sh
SKIP_QA=1 git commit ...
SKIP_QA=1 git push
```

If you use that escape hatch, explain why in the commit message or PR summary.

## Known Baseline

The Python lint gate is intentionally strict for new code, but a small
pre-existing baseline is quarantined in `pyproject.toml` under
`[tool.ruff.lint.per-file-ignores]`.

Important details:

- baseline debt lives per file, not as global rule disables
- new files do not inherit that debt
- if you fix one of those quarantined files, remove its ignore entry

You can inspect the remaining baseline with:

```sh
make lint-py-baseline-report
```

## Adding Tests

Use these rough buckets:

- `tests/test_*.py` for fast, focused offline checks
- `tests/regress/` for permanent regression tests that must stay in the fast tier
- `tests/deep/` for larger offline sweeps, fuzz/property tests, and slower cases

If a fast-tier test becomes expensive, profile it with pytest durations and
move it to `tests/deep/` or mark it `slow`.
