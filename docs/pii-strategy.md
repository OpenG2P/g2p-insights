# PII strategy

See [README §8](../README.md#8-pii-protection) for the layered defense overview.

## Adding a country-specific recognizer

1. Edit `backend/catalog/pii_recognizers.yaml` (or the deployment's overlay):
   ```yaml
   recognizers:
     - entity: FAYDA_TOKEN
       patterns:
         - {name: fayda_numeric, regex: '\b\d{12}\b', score: 0.6}
       context: [fayda, token]
   ```
2. Restart the backend OR `POST /v1/insight/catalog/reload`.
3. Verify with a probe query: `POST /v1/insight/ask` containing a sample token —
   the response `pii_redactions[]` must include the new entity type.

## Postgres role hardening (one-time)

```sql
CREATE ROLE insight_ai LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA public TO insight_ai;
GRANT SELECT ON v_individuals_safe, mv_individuals_summary, mv_district_aggregates TO insight_ai;
REVOKE SELECT ON individuals, households FROM insight_ai;
ALTER ROLE insight_ai SET statement_timeout = '30s';
ALTER ROLE insight_ai SET lock_timeout = '5s';
ALTER ROLE insight_ai SET idle_in_transaction_session_timeout = '60s';
```
