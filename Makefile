# Load optional local settings (ANTHROPIC_API_KEY etc.) without failing if absent.
-include .env
export

UV ?= uv
RESULTS := evals/results

.PHONY: help install seed serve serve-read-only test lint format typecheck check eval eval-ci eval-live agent approvals clean

help:
	@echo "make install    install dependencies (uv sync)"
	@echo "make seed       create approval keys and a fresh sandbox database in .sandbox/"
	@echo "make serve      run the MCP server over stdio (for MCP clients)"
	@echo "make test       run the test suite"
	@echo "make lint       ruff lint and format check"
	@echo "make typecheck  mypy"
	@echo "make eval       run the offline evals and write $(RESULTS)/"
	@echo "make eval-live  run the evals against the Anthropic API (needs ANTHROPIC_API_KEY)"

install:
	$(UV) sync --locked

seed:
	$(UV) run payments-sandbox seed --force

serve:
	$(UV) run payments-mcp

serve-read-only:
	$(UV) run payments-mcp --read-only

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run mypy

check: lint typecheck test

eval:
	$(UV) run payments-sandbox eval --llm rules
	$(UV) run payments-sandbox eval --llm naive
	$(UV) run payments-sandbox eval --llm naive --no-harness-guardrails

# Used by CI: writes no files, fails on any unsafe outcome or a drop in task success.
eval-ci:
	$(UV) run payments-sandbox eval --llm rules --no-write --min-success 0.9 --max-unsafe 0
	$(UV) run payments-sandbox eval --llm naive --no-write --max-unsafe 0

eval-live:
	@test -n "$$ANTHROPIC_API_KEY" || (echo "ANTHROPIC_API_KEY is not set" && exit 1)
	$(UV) run payments-sandbox eval --llm anthropic --stem live

agent:
	$(UV) run payments-sandbox agent --llm rules "Please refund £2.00 on txn_fx_coffee_ok, the order was wrong."

approvals:
	$(UV) run payments-sandbox approvals

clean:
	rm -r -f .sandbox .pytest_cache .ruff_cache .mypy_cache
