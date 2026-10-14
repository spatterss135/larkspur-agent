.PHONY: setup test lint ci sync eval label pending

setup:  ## Install Python and dependencies
	uv sync

test:  ## Run unit and integration tests (no API keys needed)
	uv run pytest

lint:  ## Check style and formatting
	uv run ruff check .
	uv run ruff format --check .

ci: lint test  ## What CI runs on every push and PR

sync:  ## Push the YAML evals to LangSmith datasets (needs LANGSMITH_API_KEY)
	uv run python -m evals.datasets

eval:  ## Run cases with the real models: make eval CASE=faq-parking [JUDGE=1]
	uv run python -m evals.run $(foreach c,$(CASE),--case $(c)) $(if $(JUDGE),--judge,)

label:  ## Label the calibration holdout in a local page: make label [ARGS=--all|--redo]
	uv run python -m evals.label $(ARGS)

pending:  ## What's waiting for a coordinator
	uv run python -m agent.cli pending
