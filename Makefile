# Folio dev helpers. Thin wrappers over docker compose.
#
# Usage: `make up`, `make init-db`, `make auth-drive ACCOUNT=you@example.com`, ...

COMPOSE ?= docker compose
ACCOUNT ?=

.DEFAULT_GOAL := help

.PHONY: help build up down logs init-db auth-drive auth-gmail sync-drive \
        discover reconcile fmt psql ps

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

build: ## Build all images
	$(COMPOSE) build

up: ## Start the stack in the background
	$(COMPOSE) up -d

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail=200

ps: ## Show service status
	$(COMPOSE) ps

init-db: ## Apply migrations + seed admin user (one-shot)
	$(COMPOSE) run --rm worker init-db

auth-drive: ## OAuth a Drive account: make auth-drive ACCOUNT=you@example.com
	$(COMPOSE) run --rm worker auth-drive --account $(ACCOUNT)

auth-gmail: ## OAuth a Gmail account: make auth-gmail ACCOUNT=you@example.com
	$(COMPOSE) run --rm worker auth-gmail --account $(ACCOUNT)

sync-drive: ## Run a Drive sync now (optionally ACCOUNT=...)
	$(COMPOSE) run --rm worker sync-drive $(if $(ACCOUNT),--account $(ACCOUNT),)

discover: ## Run Gmail sender discovery now (optionally ACCOUNT=...)
	$(COMPOSE) run --rm worker discover-senders $(if $(ACCOUNT),--account $(ACCOUNT),)

reconcile: ## Run reconciliation now (optionally ACCOUNT=...)
	$(COMPOSE) run --rm worker reconcile $(if $(ACCOUNT),--account $(ACCOUNT),)

fmt: ## Format Python with ruff (if installed)
	@command -v ruff >/dev/null 2>&1 && ruff format . || echo "ruff not installed; skipping"

psql: ## Open a psql shell on the db service
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-folio} -d $${POSTGRES_DB:-folio}
