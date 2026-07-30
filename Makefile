# LIES — single entry point for the most common dev workflow.
# Names and purposes are fixed; implementations may evolve.

UV         ?= uv
PY         := $(UV) run
SRC        := src/lies
TESTS      := tests
RUFF_LINT  := $(PY) ruff check $(SRC) $(TESTS)
RUFF_FMT   := $(PY) ruff format $(SRC) $(TESTS)
MYPY       := $(PY) mypy $(SRC)
PYTEST     := $(PY) pytest

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} \
		/^[a-zA-Z_-]+:.*?##/ {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: init
init: ## Set up environment from scratch (uv sync).
	$(UV) sync

.PHONY: sync
sync: ## Update environment to match current config.
	$(UV) sync

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

.PHONY: clean
clean: ## Remove caches and build artifacts.
	rm -rf \
		.pytest_cache .mypy_cache .ruff_cache \
		__pycache__ */__pycache__ */*/__pycache__ */*/*/__pycache__ \
		dist build *.egg-info */*.egg-info */*/*.egg-info

.PHONY: lint
lint: ## Run ruff check on src and tests.
	$(RUFF_LINT)

.PHONY: typecheck
typecheck: ## Run mypy on src.
	$(MYPY)

.PHONY: format
format: ## Run ruff format (may auto-edit).
	$(RUFF_FMT)

.PHONY: check
check: ## Run lint, typecheck, and format.
	$(RUFF_LINT)
	$(MYPY)
	$(RUFF_FMT)

.PHONY: release
release: check ## Bump version, update CHANGELOG, run checks, push tag.
	@echo "release: bump + changelog + tag not yet automated; run gates manually." >&2
	@exit 1
