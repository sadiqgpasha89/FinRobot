.PHONY: help install lint fmt test test-integration cov up down logs migrate ping serve ui eval docker
UV ?= uv

help:           ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

install:        ## install locked dependencies (dev included)
	$(UV) sync --frozen

lint:           ## ruff
	$(UV) run ruff check src tests evals

fmt:            ## apply safe lint fixes
	$(UV) run ruff check --fix src tests evals

test:           ## unit tests: no network, GPU or database
	$(UV) run pytest

test-integration: ## needs FINROBOT_TEST_DATABASE_URL (a Postgres with pgvector)
	$(UV) run pytest -m integration

cov:            ## unit tests with coverage
	$(UV) run pytest --cov --cov-report=term-missing:skip-covered

up:             ## start the stack (needs .env; see .env.example)
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api

migrate:        ## apply database migrations (the API also does this on start)
	$(UV) run finrobot migrate

ping:           ## check postgres, object store, models, tracing
	$(UV) run finrobot ping

serve:          ## API on localhost without docker
	$(UV) run finrobot serve

ui:
	$(UV) run finrobot ui

eval:           ## live evals against real models (and LangSmith when configured)
	$(UV) run python evals/run.py

docker:         ## build the image
	docker build -t finrobot:local .
