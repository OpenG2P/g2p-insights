# Superset 6.x no longer bundles database drivers in the base image (4.x did),
# and it runs from a uv-managed venv at /app/.venv that has NO pip. So we add
# the Postgres driver with `uv`, targeting the venv's python explicitly.
# In production, extend the official image the same way with the drivers you need.
FROM apache/superset:6.1.0

USER root
RUN uv pip install --python /app/.venv/bin/python --no-cache psycopg2-binary
USER superset
