# Schema catalog format

See [README §5](../README.md#5-schema-catalog--generation-and-structure) for the
canonical reference.

Quick checklist when authoring a per-deployment catalog:

1. Generate the draft:
   ```
   python backend/scripts/generate_catalog.py \
       --package <registry_extension.models> \
       --output backend/catalog/schema.yaml \
       --country-code <ISO> \
       --default-currency <XYZ>
   ```
2. Review every column's `pii` field. The heuristic is conservative — anything
   matching `name|phone|email|address|fayda|aadhaar|national_id|ssn|dob|birth_date`
   defaults to `pii`; anything matching `age|gender|marital|ethnicity|religion|language`
   defaults to `quasi`. Everything else is `safe`.
3. Add descriptive text on tables and columns. The LLM uses these for retrieval
   and for grounded SQL generation.
4. Mark indexed columns (`indexed: [...]`) and units (`unit: ETB`, `unit: kg`).
5. Add an `mv_*` materialized view per major analytical pattern. The cost gate
   prefers these heavily.
6. List `safe_views` — the read-only role can ONLY query these.
7. Map geo columns to MDS level mnemonics under `geo_columns`.
8. Commit the file. Re-running the generator OVERWRITES — diff carefully.
