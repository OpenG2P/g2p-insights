import os

# Minimal local-test Superset configuration (Superset 6.x).
#
# 6.x notes:
#  * deck.gl maps default to OpenStreetMap tiles — NO Mapbox API key needed.
#  * THEME_OVERRIDES is deprecated in 6.0 — branding now goes through
#    THEME_DEFAULT / THEME_DARK below (Ant Design v5 tokens).
#  * The embedded SDK now supports drill-to-detail inside the iframe.

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

# -----------------------------------------------------------------------------
# OpenG2P branding (see /repos/branding/guidelines.txt)
# -----------------------------------------------------------------------------
# Mirrors the Insight shell's Ant Design theme (frontend/src/theme.ts) so an
# embedded dashboard reads as part of the app rather than as a bolted-on iframe.
# Superset 6.x themes ARE Ant Design v5 token sets, and the shell is Ant Design
# too, so the same token names carry across.
#
# Deliberately NOT overridden: the categorical/sequential chart colour schemes.
# Superset's defaults are tuned for legibility; brand colours belong on the
# chrome, not on the data.
BRAND = {
    "yellow": "#F5BB1A",
    "black": "#061327",
    "orange": "#F07B1A",
    "blue": "#2A477A",
    "light_grey": "#F6F6F6",
}

# Brand fonts. fontUrls is fetched by the frontend at runtime; fonts.googleapis
# .com is already in THEME_FONT_URL_ALLOWED_DOMAINS. Same families/source as the
# shell (frontend/index.html) so type matches across tabs.
_FONT_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Roboto:wght@300;400;500;700"
    "&family=Roboto+Slab:wght@400;500;700&display=swap"
)

THEME_DEFAULT = {
    "token": {
        "brandAppName": "OpenG2P Insight",
        "brandLogoAlt": "OpenG2P Insight",
        # Yellow reads as the brand's primary the same way it does in the shell
        # (nav highlight, primary buttons). Links get the blue instead — yellow
        # text on white is nowhere near readable.
        "colorPrimary": BRAND["yellow"],
        # Text drawn ON a primary (yellow) surface. Ant Design defaults this to
        # white, which is ~1.9:1 against #F5BB1A and effectively unreadable. The
        # shell already overrides it to black (Button.primaryColor in
        # frontend/src/theme.ts); black on yellow is ~11:1.
        "colorTextLightSolid": BRAND["black"],
        "colorLink": BRAND["blue"],
        "colorInfo": BRAND["blue"],
        "colorWarning": BRAND["orange"],
        # No red/green in the brand palette; keep Superset's semantic defaults.
        "colorError": "#e04355",
        "colorSuccess": "#5ac189",
        "fontUrls": [_FONT_URL],
        "fontFamily": "'Roboto', system-ui, Helvetica, Arial, sans-serif",
        "fontFamilyCode": "'IBM Plex Mono', 'Courier New', monospace",
    },
    "algorithm": "default",
}

# The Insight shell is light-only. Forcing a single theme keeps an embedded
# dashboard from rendering dark inside a light app on a dark-mode machine.
THEME_DARK = None
