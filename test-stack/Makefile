# =============================================================================
# OpenG2P Insight — local test stack helpers
# =============================================================================
# Common workflow:
#   make up           build + start postgres, superset, backend, frontend
#   make seed-md      snapshot Ethiopia geo from the configured MDS
#   make seed         generate ~10k synthetic individuals + households
#   make refresh-mv   refresh materialized views in postgres
#   make smoke        hit the backend's /health and run a basic query
#   make logs         tail logs from all services
#   make down         stop everything
#   make clean        stop AND delete the postgres + superset volumes
# =============================================================================

SHELL := /bin/bash
COMPOSE := docker compose --env-file .env

MASTERDATA_URL ?= https://master-data.trial.openg2p.org
REGISTRY_DB_URL ?= postgresql://postgres:postgres@localhost:5433/registry

.PHONY: help up down logs ps build seed seed-md refresh-mv smoke clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

env:
	@test -f .env || (cp .env.example .env && echo "[make] wrote .env from .env.example — review before continuing")

up: env ## Build images and start all services
	$(COMPOSE) up -d --build
	@echo "[make] Stack started."
	@echo "       backend   : http://localhost:8000/v1/insight/health"
	@echo "       frontend  : http://localhost:8080"
	@echo "       superset  : http://localhost:8088   (admin/admin)"
	@echo "       postgres  : localhost:5433  (postgres/postgres)"

down: ## Stop services (keep volumes)
	$(COMPOSE) down

logs: ## Tail logs
	$(COMPOSE) logs -f --tail=100

ps: ## Show running services
	$(COMPOSE) ps

build: ## Rebuild images
	$(COMPOSE) build

seed-md: env ## Snapshot Ethiopia geo from the configured MDS
	MASTERDATA_URL=$(MASTERDATA_URL) python3 scripts/seed_master_data.py

seed: env ## Generate ~10k synthetic registry rows
	REGISTRY_DB_URL=$(REGISTRY_DB_URL) MASTERDATA_URL=$(MASTERDATA_URL) \
	python3 scripts/seed_data.py --households 2500

refresh-mv: ## Refresh materialized views
	docker exec insight-test-postgres psql -U postgres -d registry \
	  -c "REFRESH MATERIALIZED VIEW mv_individuals_summary; REFRESH MATERIALIZED VIEW mv_district_aggregates;"

smoke: ## End-to-end checks: health, catalog, NL flow, PII, cost gate, k-anonymity
	@echo "── total individuals via psql ─────────────────────────────"
	@docker exec insight-test-postgres psql -U postgres -d registry \
	  -c "SELECT COUNT(*) AS individuals FROM mv_individuals_summary;" || true
	@echo
	@echo "── integration smoke (Python) ─────────────────────────────"
	@INSIGHT_BASE_URL=http://localhost:8000/v1/insight python3 scripts/integration_smoke.py

clean: ## Stop AND wipe DB + superset volumes
	$(COMPOSE) down -v
	@rm -rf postgres/cache
