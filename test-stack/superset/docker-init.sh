#!/bin/bash
# Initialise Superset on first boot and then run the webserver.
#
# Keep this minimal: ONLY commands that work via Superset's CLI. Anything that
# touches `superset.app:create_app()` from outside the WSGI entrypoint can
# leave module state that breaks the subsequent gunicorn launch. Register the
# `insight` DB connection out-of-band via REST after the webserver is up
# (see register_superset_db.sh).
set -e

# Wait for Postgres metadata DB. pg_isready isn't installed in the Superset
# image, so we use Python's stdlib for a TCP-level wait.
until python -c "import socket,sys; s=socket.socket(); s.settimeout(3); s.connect(('postgres',5432)); s.close()" >/dev/null 2>&1; do
  echo "[superset-init] waiting for postgres..."
  sleep 2
done

export FLASK_APP=superset
export PYTHONPATH=/app/pythonpath:${PYTHONPATH:-}

# Idempotent metadata bootstrap.
superset db upgrade

if ! superset fab list-users | grep -q "${ADMIN_USERNAME:-admin}"; then
  superset fab create-admin \
    --username "${ADMIN_USERNAME:-admin}" \
    --firstname Admin \
    --lastname User \
    --email "${ADMIN_EMAIL:-admin@example.com}" \
    --password "${ADMIN_PASSWORD:-admin}"
fi

superset init

# Don't use /usr/bin/run-server.sh — it tries `gunicorn ... superset:application`
# which isn't a valid attribute in Superset 4.x. Use the app-factory form.
exec gunicorn \
    --bind 0.0.0.0:8088 \
    --workers 2 \
    --worker-class gthread \
    --threads 4 \
    --timeout 60 \
    --access-logfile - \
    --error-logfile - \
    'superset.app:create_app()'
