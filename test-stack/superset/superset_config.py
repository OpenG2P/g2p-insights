import os

# Minimal local-test Superset configuration. Production deployments use
# whatever superset.<env>.openg2p.org provides.

SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "please-change-me-in-real-deploys")
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@postgres:5432/superset"
)
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    # DASHBOARD_RBAC enforces per-dashboard owner/role access. When True,
    # Superset's REST API returns ONLY dashboards owned by the requesting
    # user or explicitly shared with their role — even Admin can lose
    # visibility after `superset init` rebuilds perms. We don't need it
    # for local dev; leave it off until a real multi-tenant deployment
    # needs it.
    "DASHBOARD_RBAC": False,
}
ENABLE_CORS = True
HTTP_HEADERS = {}
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "origins": ["*"],
}

WTF_CSRF_ENABLED = False

# Embedding: guest tokens map to the "Public" role. By default Public has no
# permissions — embedded dashboards load the layout but every chart 403s.
# Mirror the Gamma role's read-only perms onto Public so embedded views work
# without per-dataset configuration. Tighten in production by giving Public
# only the specific datasets you want embeddable.
PUBLIC_ROLE_LIKE = "Gamma"

# Allow the embedded SDK's iframe to be loaded from anywhere in dev.
# Tighten in production to the actual Insight frontend origin.
TALISMAN_ENABLED = False
SESSION_COOKIE_SAMESITE = None
SESSION_COOKIE_SECURE = False
GUEST_ROLE_NAME = "Public"
GUEST_TOKEN_JWT_EXP_SECONDS = 300
