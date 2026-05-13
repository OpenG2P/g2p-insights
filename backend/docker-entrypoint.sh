#!/bin/sh
# OpenG2P Insight backend entrypoint.
# exec so uvicorn receives SIGTERM directly from Docker for graceful shutdown.

exec uvicorn openg2p_insight.app:app \
    --host "${UVICORN_HOST:-0.0.0.0}" \
    --port "${UVICORN_PORT:-8000}" \
    --workers "${UVICORN_WORKERS:-1}" \
    --log-level "${UVICORN_LOG_LEVEL:-info}" \
    --loop asyncio
