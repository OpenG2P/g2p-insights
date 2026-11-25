#!/bin/sh
# Write the runtime configuration the app reads, from this container's env.
#
# See src/runtime-config.ts for why: Vite substitutes VITE_* at BUILD time, so a
# container environment variable cannot reach a built bundle. This bridges the
# gap — one image, configured per environment by the chart.
set -e

CONFIG=/usr/share/nginx/html/config.js

cat > "$CONFIG" <<JS
// Generated at container start from the environment. Do not edit.
window.__INSIGHT_CONFIG__ = {
  apiBaseUrl: "${VITE_API_BASE_URL:-/v1/insight}",
  supersetUrl: "${VITE_SUPERSET_URL:-}",
  evidenceUrl: "${VITE_EVIDENCE_URL:-}"
};
JS

echo "[frontend] runtime config:"
sed 's/^/[frontend]   /' "$CONFIG"

exec "$@"
