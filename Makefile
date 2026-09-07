# A convenience wrapper, not the interface. `make` is not installed on Windows by
# default, and every target below is one plain command that can be typed directly --
# docs/QUICKSTART.md spells them out that way for exactly that reason.

.DEFAULT_GOAL := help
PY ?= python
COMPOSE ?= docker compose

.PHONY: help install dev api worker scheduler migrate revision seed test test-unit \
        test-integration test-pg lint fmt check smoke up down logs psql clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Create the virtualenv and install the project with its dev extras
	$(PY) -m venv .venv
	.venv/bin/pip install -e ".[dev]" || .venv/Scripts/pip install -e ".[dev]"

# --------------------------------------------------------------------- running

dev: migrate  ## API with reload, on SQLite, no Docker required
	$(PY) -m uvicorn app.main:app --factory --reload --port 8000

api: migrate  ## API, production settings
	$(PY) -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

worker:  ## One worker process. Run as many as you like; they coordinate in the database
	$(PY) -m app.queue.worker

scheduler:  ## The single scheduler. Exactly one, ever -- see docs/RUNBOOK.md
	$(PY) -m app.queue.scheduler

# --------------------------------------------------------------------- database

migrate:  ## Apply migrations
	$(PY) -m alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="add x"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

downgrade:  ## Roll back one migration
	$(PY) -m alembic downgrade -1

seed:  ## Queue one crawl immediately (needs the API running and ADMIN_TOKEN set)
	curl -sS -X POST localhost:8000/api/v1/admin/crawl/run \
	  -H "X-Admin-Token: $${ADMIN_TOKEN}" | $(PY) -m json.tool

# --------------------------------------------------------------------- quality

test:  ## The whole suite
	$(PY) -m pytest -q

test-unit:  ## Unit tests only: no database, no network
	$(PY) -m pytest tests/unit -q

test-integration:  ## Integration tests: real migrations, real queries
	$(PY) -m pytest tests/integration -q

test-pg:  ## The same suite against PostgreSQL -- the dialect gate before release
	TEST_DATABASE_URL=postgresql+psycopg://playlens:playlens@localhost:5432/playlens_test \
	  $(PY) -m pytest -q

lint:  ## Ruff
	$(PY) -m ruff check .

fmt:  ## Ruff, fixing what it can
	$(PY) -m ruff check --fix .

check: lint test  ## What CI runs

smoke:  ## Migrate, boot the app and request every route once
	$(PY) -m scripts.smoke

demo:  ## Load the design fixture catalogue so there is something to look at
	$(PY) -m alembic upgrade head
	$(PY) -m scripts.seed_demo

browser:  ## Real Chromium over every page at 1440/1280/768/390. Needs a server running
	node scripts/browser_smoke.mjs http://127.0.0.1:8000

contract:  ## Hit the LIVE source and check it still has the shape the parser expects
	CONTRACT_TESTS=1 $(PY) -m pytest -m contract -q

release0:  ## Re-run the Release 0 measurement on the configured model
	$(PY) -m scripts.release0

# --------------------------------------------------------------------- docker

up:  ## Postgres + api + worker + scheduler (database NOT published to the host)
	$(COMPOSE) up --build -d

up-dev:  ## The same, plus PostgreSQL on 127.0.0.1:5432 for local tools
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up --build -d

up-prod:  ## Restart policies, resource limits, log rotation. NOT VERIFIED -- see the file
	$(COMPOSE) -f docker-compose.yml -f docker-compose.prod.yml up --build -d

down:  ## Stop everything, keep the volume
	$(COMPOSE) down

logs:  ## Tail everything
	$(COMPOSE) logs -f --tail=100

psql:  ## A shell on the database (works without publishing the port)
	$(COMPOSE) exec postgres psql -U playlens playlens

clean:  ## Remove caches and the local SQLite database
	rm -rf .pytest_cache .ruff_cache .cache **/__pycache__ playlens.db
