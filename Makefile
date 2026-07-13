.PHONY: install run dev lint format check test ready

install:
	uv sync --all-groups

run:
	uv run uvicorn server:app --host 127.0.0.1 --port 8123

dev:
	READER3_DEV=1 WATCHFILES_FORCE_POLLING=true uv run uvicorn server:app \
		--host 127.0.0.1 --port 8123 \
		--reload --reload-dir . --reload-delay 0.25 \
		--reload-include '*.py' --reload-include '*.html' \
		--reload-exclude '.venv/*' --reload-exclude '*_data/*'

lint:
	uv run ruff check .

format:
	uv run ruff format .

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run python -m compileall -q server.py reader3.py importers.py llm_providers.py tests

test:
	uv run python -m unittest discover -s tests -v

ready: check test
