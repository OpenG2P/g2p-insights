# g2p-insight Helm chart

This is the deployment artefact for OpenG2P Insight on Kubernetes. It mirrors the
shape of [`openg2p-audit-manager`](https://github.com/OpenG2P/audit-manager) but
covers two services (backend + frontend) from one chart.

## Layout

```
g2p-insight/
├── Chart.yaml                       depends on common@2.30.0 + postgres-init@1.1.0
├── values.yaml                       global.* + insightBackend.* + insightFrontend.*
├── questions.yaml                    Rancher catalog metadata
└── templates/
    ├── _helpers.tpl
    ├── configmap-app.yaml            /app/config/config.yaml for the backend
    ├── configmap-catalog.yaml        /app/catalog/{schema,pii_recognizers,viz_templates}.yaml
    ├── secret-llm.yaml               optional in-chart secret (off by default)
    └── frontend-deployment.yaml      inline Deployment + Service for the frontend
```

## Notes

1. The backend Deployment / Service / Istio VirtualService are rendered by the
   `common` subchart at `2.30.0`, driven by the `insightBackend.*` block —
   identical pattern to `audit-manager`'s `auditManager.*`.

2. The frontend uses an **inline** Deployment + Service in this chart
   (`templates/frontend-deployment.yaml`) since the `common` chart pattern is
   one app per chart. If your team prefers two charts, split this into:

   ```
   helm/g2p-insight-backend/
   helm/g2p-insight-frontend/
   helm/g2p-insight/        # umbrella that pulls both in as dependencies
   ```

   …and remove `frontend-deployment.yaml` from this chart.

3. The `ANTHROPIC_API_KEY` secret is **not** committed. Provide it
   out-of-band per deployment:

   ```
   kubectl create secret generic g2p-insight-llm \
       --from-literal=anthropic-api-key='sk-ant-...'
   ```

   Or supply it via sealed-secrets / external-secrets — whatever the
   environment uses. `templates/secret-llm.yaml` exists as an optional
   convenience only and is gated behind `insightBackend.createLlmSecretFromValues`.

4. The schema catalog (`schema.yaml`, `pii_recognizers.yaml`,
   `viz_templates.yaml`) is shipped via `insightBackend.appCatalog.*` blocks.
   Override per deployment in your environment's values overlay; the platform
   hot-reloads on file change.

5. Superset is **not** deployed by this chart. Point `global.supersetUrl` at
   the existing OpenG2P Superset (e.g. `https://superset.qa2.openg2p.org`).
