# OpenG2P Insight — Frontend

React + TypeScript + Vite. Themed with [Ant Design 5](https://ant.design/) and
the OpenG2P brand palette (see `src/theme.ts`).

## Local development

```bash
npm install
VITE_API_TARGET=http://localhost:8000 npm run dev
```

Opens on `http://localhost:5173`. API calls are proxied to the backend defined
by `VITE_API_TARGET`.

## Build

```bash
npm run build
```

Outputs to `dist/`. The Dockerfile builds and serves this via nginx.

## Pages

| Route | What it does |
|-------|--------------|
| `/dashboards` | Embeds Superset (configurable URL). |
| `/ask`         | Natural-language ad-hoc query (Flow A). |
| `/expert`      | Expert Lab — runs registered statistical functions (Flow C). |
| `/catalog`     | Read-only view of the schema catalog. |
| `/audit`       | Audit log of executed queries. |

## Branding

- Title bar background: `#061327` (deep navy / "almost-black").
- Logo: `public/logos/g2p-insight-dark.svg` (copied from
  `branding/logos/g2p-insight/DARK/SVG/`).
- Fonts: Roboto + Roboto Slab (loaded from Google Fonts in `index.html`).
- Full palette in `src/theme.ts`.

## Keycloak

The `src/auth/index.ts` is a no-op stub for MVP. Replace with a Keycloak adapter
(`keycloak-js` or `oidc-client-ts`) when integrating SSO.
