# =============================================================================
# OpenG2P Insight — local test stack helpers
# =============================================================================
# Common workflow:
#   make up           build + start postgres, superset, backend, frontend
#   make seed-md      snapshot Ethiopia geo from the configured MDS
#   make seed         generate ~10k synthetic individuals + households
#   make refresh-mv   refresh materialized views in postgres
#   make seed-dash    create a sample embedded dashboard in Superset
#   make maps-host    build + serve the Evidence "Maps" surface on the HOST
#   make smoke        hit the backend's /health and run a basic query
#   make logs         tail logs from all services
#   make down         stop everything
#   make clean        stop AND delete the postgres + superset volumes
# =============================================================================

SHELL := /bin/bash
# Use `docker compose` (plugin) if available, else fall back to legacy `docker-compose`.
COMPOSE_BIN := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
COMPOSE := $(COMPOSE_BIN) --env-file .env

# Host-side scripts (seed-md, seed) reach the mock MDS via its PUBLISHED port —
# the compose hostname `mock-mds` only resolves inside the docker network.
MASTERDATA_URL ?= http://localhost:8090
INSTALL_AI ?= false
REGISTRY_DB_URL ?= postgresql://postgres:postgres@localhost:5433/registry
SUPERSET_URL ?= http://localhost:8088

.PHONY: help up down logs ps build seed seed-md seed-dash refresh-mv smoke clean ui maps maps-host

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

env:
	@test -f .env || (cp .env.example .env && echo "[make] wrote .env from .env.example — review before continuing")

up: env ## Start the lean stack (no Superset) — fits comfortably on a laptop
	$(COMPOSE) up -d --build
	@echo "[make] Backend stack started (frontend + Superset NOT in Docker)."
	@echo "       backend   : http://localhost:8000/v1/insight/health"
	@echo "       mock MDS  : http://localhost:8090/health"
	@echo "       postgres  : localhost:5433  (postgres/postgres)"
	@echo ""
	@echo "       Run the UI with a live dev loop:   make ui   (-> http://localhost:5173)"
	@echo "       Add Superset:  make up-full     Full AI/PII pipeline:  make up-ai"

up-full: env ## Start everything INCLUDING Superset (needs ~6GB for Docker)
	SUPERSET_URL=http://superset:8088 $(COMPOSE) --profile with-superset up -d --build
	@echo "[make] Backend + Superset started (frontend via `make ui`)."
	@echo "       backend   : http://localhost:8000/v1/insight/health"
	@echo "       superset  : http://localhost:8088   (admin/admin)"
	@echo "       mock MDS  : http://localhost:8090/health"
	@echo "       postgres  : localhost:5433  (postgres/postgres)"
	@echo "       UI        : make ui   (-> http://localhost:5173)"

up-ai: env ## Start the lean stack WITH the full AI/PII pipeline (spaCy, Presidio, embeddings) — heavier build (~2-3GB image, pulls torch)
	INSTALL_AI=true $(COMPOSE) up -d --build
	@echo "[make] Stack started with the full AI pipeline (spaCy NER + embeddings)."
	@echo "       frontend  : http://localhost:8080"
	@echo "       backend   : http://localhost:8000/v1/insight/health"

ui: ## Run the frontend dev server (Vite) on http://localhost:5173 with live errors
	@echo "[make] Installing UI deps (first run only) and starting Vite..."
	cd ../frontend && (test -d node_modules || npm install) && VITE_EVIDENCE_URL=http://localhost:3100 VITE_SUPERSET_URL=http://localhost:8088 npm run dev

maps: ## Build & run the Evidence "Maps" surface on http://localhost:3100 (heavier build)
	INSTALL_AI=false $(COMPOSE) --profile with-evidence up -d --build evidence
	@echo "[make] Evidence Maps started at http://localhost:3100 (embedded by the Insight Maps tab)."

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

seed-dash: ## Create a sample embedded dashboard in Superset (needs Superset up)
	python3 scripts/seed_superset.py --clean $(SUPERSET_URL)
	@echo "[make] 'Registry Overview' dashboard seeded + embedded — open the Dashboards tab."

maps-host: ## Build the Evidence Maps site and serve it from the HOST on :3100 (avoids Colima OOM)
	@echo "[make] Building Evidence (sources -> build) and serving on http://localhost:3100 ..."
	cd evidence && (test -d node_modules || npm install) && npm run sources && npm run build
	@echo "[make] Serving — leave this running; the Insight Maps tab embeds it."
	cd evidence && npx serve@14 -s build -l 3100

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
