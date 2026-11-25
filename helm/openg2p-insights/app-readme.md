# OpenG2P Insights

The analytics surface for OpenG2P registries: governed dashboards, drill-down
maps, and ad-hoc questions in plain language — one install per environment.

## Three surfaces

**Dashboards** embeds Apache Superset in the OpenG2P shell, so analysts get
Superset's full charting and row-level security without leaving Insights.

**Maps** is a static [Evidence](https://evidence.dev) site: click a region to load
its districts, click a district for its wards, with the same accountability
metrics at every level compared against the level above. Boundaries come from the
country pack the environment's Master Data Service was seeded with.

**Ask** turns a question into SQL, validates it before it runs, estimates its cost,
and applies k-anonymity to the answer. The generated SQL is always shown.

## What it expects to already exist

Insights integrates; it does not stand up its own stack. Before installing:

| Needs | Why |
|---|---|
| **Apache Superset**, reachable from the browser | Dashboards embeds it and mints guest tokens against it. Users sign in to Superset through the environment's own Keycloak. Set `global.supersetUrl`. |
| **A Superset-local service account** | The API lists dashboards and mints guest tokens as a machine. Under `AUTH_TYPE = AUTH_OAUTH` only `provider=db` works, so this must be a Superset-local account — a Keycloak user cannot authenticate a service. Supply its password in `global.supersetServiceAccountSecret`. |
| **Master Data Service with a country pack loaded** | Geography — the hierarchy and P-codes the maps and geo filters join on. Declared once per environment by `geoSeed.countryPack` in the `openg2p-master-data` chart. |
| **A registry with reporting views** | The data. NSR's analytics jobs create `nsr_rpt_household` / `nsr_rpt_individual` and import its dashboards. |
| **An OpenRouter API key** | Only when `global.llmEnabled` is true. |

## Air-gapped and data-sovereign deployments

Set `global.llmEnabled=false`. No outbound model call is ever made, no API key is
needed, the Ask tab is hidden, and the `/ask` endpoints return 503. Dashboards,
Maps, Catalog and Audit are unaffected. `global.llmModel` also accepts any model
OpenRouter exposes, including self-hostable open-weight ones, for sites that want
AI features without a frontier provider.

## Authentication

Insights does not yet authenticate its own users — every visitor is treated as an
administrator. **Do not expose it on a public gateway.** Keep it on the internal
gateway until Keycloak integration lands. Superset's own login is unaffected.
