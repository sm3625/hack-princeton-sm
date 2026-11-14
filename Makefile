.PHONY: setup dev test test-ollama test-haiku lint

setup:
	uv sync
	git config core.hooksPath scripts/hooks
	chmod +x scripts/hooks/pre-commit
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

dev:
	uv run uvicorn station.app:app --host 127.0.0.1 --port $${PORT:-8000} --reload

test:
	uv run pytest -q

test-ollama:
	uv run pytest -q -m ollama

test-haiku:
	uv run pytest -q -m haiku

lint:
	uv run ruff check .
