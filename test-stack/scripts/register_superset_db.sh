#!/bin/bash
# Idempotently register the `insight` database connection in the running
# Superset, using its REST API. Run this AFTER `make up` and AFTER Superset
# becomes reachable.
#
# Usage:
#   ./register_superset_db.sh [base_url] [admin] [password]
#
# Defaults: http://localhost:8088 admin admin
set -e

BASE="${1:-http://localhost:8088}"
USER="${2:-admin}"
PASS="${3:-admin}"

echo "[register-db] logging in to $BASE as $USER"
TOKEN=$(curl -sS -X POST "$BASE/api/v1/security/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$PASS\",\"provider\":\"db\",\"refresh\":true}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")
if [ -z "$TOKEN" ]; then
  echo "[register-db] ERROR: login failed" >&2
  exit 1
fi

CSRF=$(curl -sS -X GET "$BASE/api/v1/security/csrf_token/" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('result',''))")

# Skip if already present.
EXISTS=$(curl -sS "$BASE/api/v1/database/" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(any(r.get('database_name')=='insight' for r in d.get('result',[])))")

if [ "$EXISTS" = "True" ]; then
  echo "[register-db] 'insight' database already registered — nothing to do."
  exit 0
fi

# Register via REST. The URI uses the `insight_ai` read-only role.
curl -sS -X POST "$BASE/api/v1/database/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRFToken: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{
        "database_name": "insight",
        "sqlalchemy_uri": "postgresql+psycopg2://insight_ai:insight@postgres:5432/registry",
        "expose_in_sqllab": true,
        "allow_run_async": false,
        "allow_ctas": false,
        "allow_cvas": false,
        "allow_dml": false
      }' \
  | python3 -m json.tool
echo "[register-db] done."
