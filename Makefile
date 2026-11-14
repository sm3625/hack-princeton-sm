.PHONY: setup keys dev test test-ollama test-haiku test-e2e lint eval eval-quick

setup:
	uv sync
	git config core.hooksPath scripts/hooks
	chmod +x scripts/hooks/pre-commit
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")
	$(MAKE) keys

keys:
	uv run python -m node.keys

dev:
	uv run uvicorn station.app:app --host 127.0.0.1 --port $${PORT:-8000} --reload

test:
	uv run pytest -q

test-ollama:
	uv run pytest -q -m ollama

test-haiku:
	uv run pytest -q -m haiku

test-e2e:
	uv run pytest -q -m e2e

lint:
	uv run ruff check .

eval:
	uv run python -m eval.run --all --mode mock

eval-quick:  ## 4 Night 1 incidents on the local model, under 10 min
	LLM_MODE=ollama uv run python -m eval.quick

.PHONY: photon
photon:  ## Preview pending ranger messages locally; never calls Photon
	uv run python -m station.photon
