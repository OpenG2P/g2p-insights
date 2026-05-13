#!/bin/bash
# Initialise Superset on first boot and then run the webserver.
set -e

# Wait for Postgres metadata DB
until pg_isready -h postgres -p 5432 -U postgres >/dev/null 2>&1; do
  echo "[superset-init] waiting for postgres..."
  sleep 2
done

export FLASK_APP=superset
export PYTHONPATH=/app/pythonpath:${PYTHONPATH:-}

# Idempotent admin + DB bootstrap
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

# Pre-register the registry database as a Superset connection so the AI
# agent can create charts against it without manual setup.
python - <<'PY'
import os
from superset import db
from superset.models.core import Database

uri = "postgresql+psycopg2://insight_ai:insight@postgres:5432/registry"
existing = db.session.query(Database).filter_by(database_name="insight").first()
if not existing:
    d = Database(database_name="insight", sqlalchemy_uri=uri, expose_in_sqllab=True)
    db.session.add(d)
    db.session.commit()
    print("[superset-init] registered 'insight' database connection")
else:
    print("[superset-init] 'insight' database connection already present")
PY

exec /usr/bin/run-server.sh
