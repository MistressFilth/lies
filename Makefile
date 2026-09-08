# LIES — single entry point for the most common dev workflow.
# Names and purposes are fixed; implementations may evolve.

UV         ?= uv
PY         := $(UV) run
SRC        := src/lies
TESTS      := tests
RUFF_LINT  := $(PY) ruff check $(SRC) $(TESTS)
RUFF_FMT   := $(PY) ruff format $(SRC) $(TESTS)
TY         := $(PY) ty check $(SRC)
# Default-on PG (PG001-003) + PYD only. flake8's `--select=PG` is a
# prefix match that would also pull in the opt-in `PG101` advisory
# (`BaseModel` uses no Pydantic surface), which produces a high
# false-positive rate in this repo. Pin the explicit default-on list
# here; `PG101` lives in `PG_LINT_STRICT` below for opt-in review.
PG_LINT        := $(PY) flake8 --select=PG001,PG002,PG003,PYD $(SRC)
PG_LINT_STRICT := $(PY) flake8 --select=PG101 $(SRC)
PYTEST     := $(PY) pytest

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
features-test: ## Run behavior/feature/integration tests.
	@if [ -d "$(TESTS)/features" ]; then \
		$(PYTEST) $(TESTS)/features/; \
	else \
		$(PYTEST) $(TESTS)/integration/; \
	fi

.PHONY: test
test: ## Run all tests (unit + features/integration).
	$(PYTEST)

.PHONY: test-timer
test-timer: ## Run unit tests with per-test timing; prints N slowest (override N=20).
	@N=$${N:-20}; echo "==> pytest tests/unit/ --durations=$$N (top $$N slowest)"; \
	$(PYTEST) $(TESTS)/unit/ --durations=$$N -vv --durations-min=0.0

.PHONY: clean
clean: ## Remove caches and build artifacts.
	rm -rf \
		.pytest_cache .ty_cache .ruff_cache \
		__pycache__ */__pycache__ */*/__pycache__ */*/*/__pycache__ \
		dist build *.egg-info */*.egg-info */*/*.egg-info

.PHONY: lint
lint: ## Run ruff check on src and tests.
	$(RUFF_LINT)

.PHONY: lint-pydantic-guidance
lint-pydantic-guidance: ## Run flake8 with default-on PG (PG001-003) + PYD on src.
	$(PG_LINT)

.PHONY: lint-pg101
lint-pg101: ## Run flake8 with the opt-in PG101 advisory on src.
	$(PG_LINT_STRICT)

.PHONY: typecheck
typecheck: ## Run ty on src.
	$(TY)

.PHONY: format
format: ## Run ruff format (may auto-edit).
	$(RUFF_FMT)

.PHONY: check
check: ## Run lint, pydantic-guidance lint, typecheck, and format.
	$(RUFF_LINT)
	$(PG_LINT)
	$(TY)
	$(RUFF_FMT)

.PHONY: check-strict
check-strict: check lint-pg101 ## Run check plus the opt-in PG101 advisory.

.PHONY: release
release: check test ## Bump version, update CHANGELOG, run gates, push tag.
	$(UV) run python scripts/release.py $(if $(BUMP),--bump $(BUMP),)
