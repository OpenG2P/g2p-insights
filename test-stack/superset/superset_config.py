import os

# Minimal local-test Superset configuration. Production deployments use
# whatever superset.<env>.openg2p.org provides.

SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "please-change-me-in-real-deploys")
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@postgres:5432/superset"
)
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_RBAC": True,
}
ENABLE_CORS = True
HTTP_HEADERS = {}
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "origins": ["*"],
}

WTF_CSRF_ENABLED = False
