# LIES — single entry point for the most common dev workflow.
# Names and purposes are fixed; implementations may evolve.

UV         ?= uv
PY         := $(UV) run
SRC        := src/lies
TESTS      := tests
PYTEST     := $(PY) pytest
# Every lint / typecheck / format target below goes through pre-commit,
# because `.pre-commit-config.yaml` is the single definition of what
# those gates are. Inlining the raw tool calls here meant the Makefile
# and the hook config could drift, and `ruff format --check .` had a
# file failing repo-wide that no target ever looked at.
PRECOMMIT  := $(PY) pre-commit
# Retained for ad-hoc local use; the gate targets deliberately do not
# use these, so the two definitions cannot diverge.
RUFF_LINT  := $(PY) ruff check $(SRC) $(TESTS)
RUFF_FMT   := $(PY) ruff format $(SRC) $(TESTS)
TY         := $(PY) ty check $(SRC)
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
	$(PYTEST) $(TESTS)/unit/ \
		--ignore=$(abspath $(TESTS)/unit/cli/test_query_cli.py) \
		--ignore=$(abspath $(TESTS)/mcp/test_tools.py)

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

# `--runslow` changes *which tests run*, so a durations table from the
# full run is not comparable to the gate's: the slow-marked tests the
# gate exempts are exactly the ones above 0.15 s, so they dominate the
# table and make a clean gate look like a wall of violations. Pass
# `RUNSLOW=` to `time-unit-tests` to measure the same population the
# pre-commit hook runs, budget gate included.
RUNSLOW ?= --runslow

# `tools/qmd_lies_gate.py` is the only producer of the fixture's
# `lies_gate` block. Unwired, that block is a constant nobody can
# regenerate and `test_lies_gate_slot_is_reserved` keeps passing against
# a number with no source. A Make target rather than a pre-commit hook:
# it needs a live daemon, and a recall measurement in a commit hook is
# the kind of gate that flakes and then gets bypassed. It compares
# rather than writing -- `qmd_bench_fixture.py` needs `--force` to
# overwrite a populated baseline, and a target that can silently clobber
# one is a hazard.
.PHONY: lies-gate
lies-gate: ## Re-measure LIES-routing recall against the committed lies_gate block.
	$(PY) tools/qmd_lies_gate.py --compare $(TESTS)/fixtures/qmd_bench.json

.PHONY: time-unit-tests
time-unit-tests: ## Run unit tests; print per-test ms. RUNSLOW= for the pre-commit population (see below).
	@echo "population: $(if $(RUNSLOW),all unit tests including slow-marked (gate exempts those),pre-commit population only — excludes --runslow; this is what the pre-commit hook runs)"
	$(PYTEST) $(TESTS)/unit/ $(RUNSLOW) --durations=0 --durations-min=0 -vv --tb=short --no-header

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
lint: ## Run ruff check via the pre-commit hook.
	$(PRECOMMIT) run --all-files ruff-check

.PHONY: lint-supyrliminal
lint-supyrliminal: ## Run supyrliminal via the pre-commit hook.
	$(PRECOMMIT) run --all-files supyrliminal

.PHONY: typecheck
typecheck: ## Run ty via the pre-commit hook.
	$(PRECOMMIT) run --all-files ty

.PHONY: format
format: ## Run ruff format via the pre-commit hook (may auto-edit).
	$(PRECOMMIT) run --all-files ruff-format

.PHONY: check
check: ## Run the full gate stack: every pre-commit hook, in order.
	$(PRECOMMIT) run --all-files ruff-check
	$(PRECOMMIT) run --all-files ruff-format
	$(PRECOMMIT) run --all-files ty
	$(PRECOMMIT) run --all-files supyrliminal
	$(MAKE) unit-test

.PHONY: qmd-backend-check
qmd-backend-check: ## Verify node-llama-cpp's CUDA backend: VMM out AND GPU up.
	## Not part of `check` because it probes the host's GPU and takes
	## ~10s, over the per-test budget. The unit suite covers the VMM
	## half; this covers the half that a plain symbol check cannot -- an
	## abort count of zero is also what "the GPU never came up" looks
	## like, so both have to be confirmed together.
	bash tools/nlc_novmm.sh verify

.PHONY: qmd-backend-fix
qmd-backend-fix: ## Rebuild the CUDA backend with VMM compiled out, then verify.
	bash tools/nlc_novmm.sh apply

.PHONY: release
release: check ## Bump version, update CHANGELOG, run gates, push tag.
	$(UV) run python scripts/release.py $(if $(BUMP),--bump $(BUMP),)
