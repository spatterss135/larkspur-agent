.PHONY: setup test lint ci

setup:  ## Install Python and dependencies
	uv sync

test:  ## Run unit and integration tests (no API keys needed)
	uv run pytest

lint:  ## Check style and formatting
	uv run ruff check .
	uv run ruff format --check .

ci: lint test  ## What CI runs on every push and PR
