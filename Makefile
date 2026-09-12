# LIES — single entry point for the most common dev workflow.
# Names and purposes are fixed; implementations may evolve.

UV         ?= uv
PY         := $(UV) run
SRC        := src/lies
TESTS      := tests
RUFF_LINT  := $(PY) ruff check $(SRC) $(TESTS)
RUFF_FMT   := $(PY) ruff format $(SRC) $(TESTS)
TY         := $(PY) ty check $(SRC)
PYTEST     := $(PY) pytest
SL         := $(PY) flake8 --select=SL,PYD $(SRC) $(TESTS)

REPO_ROOT              ?= $(HOME)/code/github/MistressFilth/lies

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} \
		/^[a-zA-Z_-]+:.*?##/ {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: init
init: ## Set up environment from scratch (uv sync).
	$(UV) sync --extra dev

.PHONY: sync
sync: ## Update environment to match current config.
	$(UV) sync --extra dev

.PHONY: unit-test
unit-test: ## Run unit tests only.
	$(PYTEST) $(TESTS)/unit/

.PHONY: features-test
features-test: ## Run behavior/feature/integration tests (requires INTEGRATION=1).
	@if [ "$$INTEGRATION" != "1" ]; then \
		echo "integration tests skipped (set INTEGRATION=1 to run)"; \
	else \
		if [ -d "$(TESTS)/features" ]; then \
			$(PYTEST) $(TESTS)/features/; \
		else \
			$(PYTEST) $(TESTS)/integration/; \
		fi; \
	fi

.PHONY: test
test: ## Run all tests (unit + features/integration).
	$(PYTEST)

.PHONY: time-unit-tests
time-unit-tests: ## Run unit tests; print per-test ms (verbose; no duration floor).
	$(PYTEST) $(TESTS)/unit/ --runslow --durations=0 --durations-min=0 -vv --tb=short --no-header

.PHONY: time-features-tests
time-features-tests: ## Run integration tests; print per-test ms (verbose; requires INTEGRATION=1).
	@if [ "$$INTEGRATION" != "1" ]; then \
		echo "integration tests skipped (set INTEGRATION=1 to run)"; \
	else \
		if [ -d "$(TESTS)/features" ]; then \
			$(PYTEST) $(TESTS)/features/ --durations=0 --durations-min=0 -vv --tb=short --no-header; \
		else \
			$(PYTEST) $(TESTS)/integration/ --durations=0 --durations-min=0 -vv --tb=short --no-header; \
		fi; \
	fi

.PHONY: clean
clean: ## Remove caches and build artifacts.
	rm -rf \
		.pytest_cache .ty_cache .ruff_cache \
		__pycache__ */__pycache__ */*/__pycache__ */*/*/__pycache__ \
		dist build *.egg-info */*.egg-info */*/*.egg-info

.PHONY: lint
lint: ## Run ruff check on src and tests.
	$(RUFF_LINT)

.PHONY: lint-supyrliminal
lint-supyrliminal: ## Run supyrliminal (SL + PYD flake8) on src and tests.
	$(SL)

.PHONY: typecheck
typecheck: ## Run ty on src.
	$(TY)

.PHONY: format
format: ## Run ruff format (may auto-edit).
	$(RUFF_FMT)

.PHONY: check
check: ## Run full pre-commit stack (ruff + format + ty + supyrliminal + unit-test).
	$(PY) ruff check --fix $(SRC) $(TESTS)
	$(PY) ruff format $(SRC) $(TESTS)
	$(TY)
	$(SL)
	$(PYTEST) $(TESTS)/unit/

.PHONY: release
release: check ## Bump version, update CHANGELOG, run gates, push tag.
	$(UV) run python scripts/release.py $(if $(BUMP),--bump $(BUMP),)
