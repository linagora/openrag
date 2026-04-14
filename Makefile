.PHONY: lint run run-cpu run-dev run-dev-cpu \
        run-build run-cpu-build run-dev-build run-dev-cpu-build \
        down logs ps

COMPOSE     := docker compose
COMPOSE_DEV := docker compose -f docker-compose.yaml -f docker-compose.dev.yaml

# Prod (default) — services auto-restart after reboot
run:
	$(COMPOSE) up -d

run-cpu:
	$(COMPOSE) --profile cpu up -d

run-build:
	$(COMPOSE) up -d --build

run-cpu-build:
	$(COMPOSE) --profile cpu up -d --build

# Dev — no auto-restart
run-dev:
	$(COMPOSE_DEV) up -d

run-dev-cpu:
	$(COMPOSE_DEV) --profile cpu up -d

run-dev-build:
	$(COMPOSE_DEV) up -d --build

run-dev-cpu-build:
	$(COMPOSE_DEV) --profile cpu up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

lint:
	uv run ruff check openrag/ tests/ && uv run ruff format --check openrag/ tests/
