#!/bin/bash
# =============================================================================
# Run the backend natively against the dockerised Postgres + Superset.
# Skips the Docker rebuild cycle. Hot-reload on every Python save.
#
# Prereqs (one-time):
#   1. Test stack must be up: `cd ../test-stack && docker-compose up -d postgres superset`
#   2. Set up the backend venv:
#        cd backend
#        python3 -m venv .venv
#        source .venv/bin/activate
#        pip install -e . --quiet
#        pip install greenlet --quiet
#   3. Stop the dockerised backend if running so port 8000 is free:
#        cd ../test-stack
#        docker-compose --env-file .env stop backend
#
# Usage:
#   export ANTHROPIC_API_KEY=sk-ant-...        # (or set INSIGHT_LLM__API_KEY)
#   ./scripts/dev_run.sh
#
# The script auto-detects backend/.venv and uses it if present.
# =============================================================================
set -e

cd "$(dirname "$0")/.."

# ---- venv ---------------------------------------------------------------------
if [ -d .venv ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi
PY=$(command -v python)
echo "[dev_run] using python: $PY"

# ---- env vars ----------------------------------------------------------------
# Match what test-stack/docker-compose.yaml sets, but point at localhost ports.

# DB (matches the dockerised Postgres on 5433)
export DB_HOST="${DB_HOST:-localhost}"
export DB_PORT="${DB_PORT:-5433}"
export DB_NAME="${DB_NAME:-registry}"
export DB_USER="${DB_USER:-postgres}"
export DB_PASSWORD="${DB_PASSWORD:-postgres}"

# openg2p-fastapi-common reads its own Settings (env_prefix=common_)
export COMMON_OPENAPI_COMMON_API_PREFIX="${COMMON_OPENAPI_COMMON_API_PREFIX:-/v1/insight}"
export COMMON_DB_DATASOURCE="${COMMON_DB_DATASOURCE:-postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}}"

# Config + catalog (use the test-stack catalog the dockerised backend was using)
export CONFIG_PATH="${CONFIG_PATH:-$(pwd)/config/default.yaml}"
export CATALOG_PATH="${CATALOG_PATH:-$(pwd)/../test-stack/postgres/catalog}"

# External services (point at dockerised ports on localhost)
export INSIGHT_SUPERSET__URL="${INSIGHT_SUPERSET__URL:-http://localhost:8088}"
export INSIGHT_SUPERSET__USERNAME="${INSIGHT_SUPERSET__USERNAME:-admin}"
export INSIGHT_SUPERSET__PASSWORD="${INSIGHT_SUPERSET__PASSWORD:-admin}"
export INSIGHT_MASTERDATA__URL="${INSIGHT_MASTERDATA__URL:-https://master-data.trial.openg2p.org}"

# LLM — caller should export ANTHROPIC_API_KEY before running this script.
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "[dev_run] WARNING: ANTHROPIC_API_KEY not set — falling back to deterministic stub"
fi
export INSIGHT_LLM__MODEL="${INSIGHT_LLM__MODEL:-claude-opus-4-7}"

# Uvicorn
export UVICORN_HOST="${UVICORN_HOST:-0.0.0.0}"
export UVICORN_PORT="${UVICORN_PORT:-8000}"
export UVICORN_LOG_LEVEL="${UVICORN_LOG_LEVEL:-info}"

echo "[dev_run] catalog: $CATALOG_PATH"
echo "[dev_run] db:      $DB_HOST:$DB_PORT/$DB_NAME"
echo "[dev_run] superset: $INSIGHT_SUPERSET__URL"
echo "[dev_run] starting uvicorn with --reload (Ctrl-C to stop)"
echo

exec uvicorn openg2p_insight.app:app \
    --host "$UVICORN_HOST" \
    --port "$UVICORN_PORT" \
    --log-level "$UVICORN_LOG_LEVEL" \
    --reload \
    --reload-dir src
