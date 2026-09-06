# CORTEX. `make up` is the whole quickstart.
#
# Every target is safe to re-run: the schema is idempotent and the containers are
# recreated rather than mutated.

COMPOSE := docker compose

.PHONY: help up down restart migrate verify logs ps clean test

help:                ## Show this help
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

up:                  ## Start everything and apply the schema
	$(COMPOSE) up -d --wait surrealdb
	$(COMPOSE) run --rm migrate

down:                ## Stop everything, keep the data
	$(COMPOSE) down

restart: down up     ## Stop and start again

migrate:             ## Re-apply db/schema.surql (idempotent)
	$(COMPOSE) run --rm migrate

verify:              ## Prove the schema does what docs/03 claims
	python db/verify.py

logs:                ## Tail the database log
	$(COMPOSE) logs -f surrealdb

ps:                  ## Show container status
	$(COMPOSE) ps

clean:               ## Stop everything and DELETE the database volume
	$(COMPOSE) down -v

test:                ## Run the API test suite
	cd api && python -m pytest -q
