.PHONY: help sync clean test-unit test typecheck fix check bump

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Install dependencies
	uv sync

test-unit: sync ## Run pytest unit tests
	uv run pytest -v; test $$? -eq 0 -o $$? -eq 5

test: test-unit ## Run all tests

typecheck: sync ## Type-check the package source
	uv run mypy pydantic_guidance

fix: ## Run pre-commit hooks with autofix (ruff check --fix + ruff-format)
	uvx pre-commit run --all-files

check: fix test typecheck ## Run all quality checks

bump: ## Bump .pre-commit-config.yaml revs to the latest tags
	uvx pre-commit autoupdate

clean: ## Remove build and cache artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
