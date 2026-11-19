# Quality + build entry points. Lint/tests use .venv-test binaries when
# present, else whatever is on PATH.
VENV := .venv-test/bin
RUFF := $(VENV)/ruff
PYTEST := $(VENV)/pytest
PY ?= python3

.PHONY: all lint lint-fix format test build zip verify smoke

all: lint test build

lint:
	$(RUFF) check klipperformac bin scripts tests
	$(RUFF) format --check klipperformac bin scripts tests --quiet

lint-fix:
	$(RUFF) check --fix klipperformac bin scripts tests
	$(RUFF) format klipperformac bin scripts tests --quiet

test:
	$(PYTEST)

build:
	cd KlipperMac && ./build-app.sh

zip:
	cd KlipperMac && KLIPPERFORMAC_ZIP=1 ./build-app.sh

verify: zip
	$(PY) scripts/verify_bundle.py

smoke:
	@$(PY) -c "from klipperformac import cli, paths, versions; print('imports ok')"
	@$(PY) -m klipperformac data >/dev/null && echo "cli data ok"