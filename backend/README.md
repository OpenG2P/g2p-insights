# OpenG2P Insight — Backend

FastAPI service built on [openg2p-fastapi-common](https://github.com/OpenG2P/openg2p-fastapi-common).

See the top-level [README](../README.md) for architecture and design.

## Local development

```bash
# 1. Install. The AI/PII pipeline (spaCy, Presidio, sentence-transformers,
#    statsmodels) is an OPTIONAL extra — omit it and the app runs with graceful
#    fallbacks (regex PII, keyword schema retrieval).
pip install -e .[test]                       # base + test deps
pip install -e .[ai] && python -m spacy download en_core_web_lg   # full pipeline (heavier)

# 2. Bring up Postgres + the service
export OPENROUTER_API_KEY=sk-or-...   # optional in early dev; falls back to deterministic stub
docker compose up --build

# 3. Apply migrations
alembic -c migrations/alembic.ini upgrade head

# 4. Drop a catalog
cp catalog/schema.example.yaml catalog/schema.yaml
cp catalog/pii_recognizers.example.yaml catalog/pii_recognizers.yaml

# 5. Smoke-test
curl http://localhost:8000/v1/insight/health
```

## Generating a catalog from a registry

```bash
python scripts/generate_catalog.py \
    --package openg2p_registry_farmer_extension.register_domain.models \
    --output catalog/schema.yaml \
    --country-code ET \
    --default-currency ETB
```

Review the generated file carefully — the PII heuristic is intentionally
conservative and over-classifies. See [docs/catalog-format.md](../docs/catalog-format.md).

## Tests

```bash
pytest
```

## Layout

See top-level README §3.
