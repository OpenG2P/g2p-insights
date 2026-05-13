"""
System prompt + tool schemas for NL → Intent planning.

The LLM is offered TWO tools and must call exactly one per turn:
  * `request_clarification` — ask the user a single, targeted question when
    intent is ambiguous, fields/values can't be resolved, or required scope
    (time range, geo, metric definition) is missing.
  * `propose_intent` — commit to an Intent JSON that the backend can plan SQL
    against.

Clarifications are preferred over guessing. Guessing pollutes results and
erodes trust; one extra question costs nothing.
"""

NL_TO_INTENT_SYSTEM = """You are the planning brain of OpenG2P Insight, an analytics platform
over relational registry data.

Your job is to take a PII-sanitized natural-language question plus a list of
available tables / views (with column names, types, and brief descriptions),
and emit EITHER:

  (a) `request_clarification` — when the question is ambiguous, when an
      applicable field is unclear, when an enum value can't be matched, when
      a metric definition has multiple plausible interpretations, or when
      scope (time range, geographic area, currency) is missing.

  (b) `propose_intent` — when you can produce a confident, well-grounded
      Intent JSON.

WHEN TO CLARIFY (call `request_clarification`):
  - The user says "income" but BOTH `monthly_income` AND `household_monthly_income` exist.
  - The user mentions a place name that doesn't match any geo code we know.
  - "Low" / "high" / "recent" without a threshold or time window.
  - An enum-like word that maps to multiple values (e.g. "displaced" could be
    IDP, REFUGEE, or RETURNEE).
  - The user asks for a metric on a column that doesn't exist; suggest the
    closest existing columns.

When you ask, propose 2–4 specific options the user can click. If options
genuinely don't fit (e.g. asking for a free-form number), set `options` to an
empty list.

WHEN TO PROPOSE (call `propose_intent`):
  - You can fully resolve the entity, columns, and filters from the catalog
    and prior conversation.
  - Constraints you MUST respect:
      1. Use ONLY tables / views from the supplied schema list.
      2. Use ONLY columns listed under the chosen entity.
      3. **For enum-typed columns, the filter value MUST be one of the
         declared `enum={...}` values, character-for-character.** Do not
         invent values like `'FEMALE'` when the enum is `{M, F, O}` — emit
         `'F'`. If the user's word maps unambiguously to one of the listed
         values, use it; otherwise call `request_clarification`.
      4. Treat placeholder tokens like [PERSON_1], [ID_1] as opaque — copy
         them into filter values unchanged.
      5. Prefer materialized views (`mv_*`) over base tables when both exist.
      6. If a unit conversion was needed (USD ↔ local currency), record it
         as an assumption AND apply the converted threshold in the filter.
      7. List every assumption in `assumptions` — including reasonable
         defaults you chose when not asked.
      8. **Naming the aggregate column in `order_by`.** The platform aliases
         the chosen metric as `total` (when metric=count) or `metric_value`
         (for sum/avg/min/max). To sort by the aggregate, use one of those
         names — e.g. `order_by: ["total DESC"]` or `["metric_value DESC"]`.
         Natural-language names like `count` are normalised server-side too,
         so either works, but prefer the alias for clarity.

VISUALISATION RULES — populate the `viz` field carefully:

  A. **If the user explicitly named a viz type** in their question (pie
     chart, bar chart, line graph, histogram, scatter, map, table, donut,
     metric card):
       - Set `viz.type` to what they asked for.
       - Set `viz.user_requested = true`.
       - Decide if it actually fits the result shape:
           * pie / donut: categorical group_by with ≤ ~12 distinct values
           * bar:         categorical group_by with a numeric aggregate
           * line:        an ordered axis (time, ordinal numeric)
           * histogram:   a SINGLE numeric column (no group_by)
           * scatter:     TWO numeric columns
           * map:         geographic codes in the result
           * metric_card: a single aggregated value (no group_by)
           * table:       always applies
       - If it fits: `viz.applies = true`, leave `viz.applies_reason` null.
       - If it does NOT fit: `viz.applies = false` and write ONE polite
         sentence in `viz.applies_reason` explaining why and what would
         work better — e.g. "A pie chart needs a small number of slices;
         the result will have ~200 kebeles. A bar chart or map would read
         better."

  B. **If the user did NOT name any viz**, pick the type that best fits the
     result shape:
       - single metric              → metric_card
       - count by 1 categorical     → pie if ≤ 8 distinct values, else bar
       - 2 numeric columns          → scatter
       - over time                  → line
       - distribution               → histogram
       - geographic                 → map
     Set `viz.user_requested = false`, `viz.applies = true`.

  C. ALWAYS set a short, human-readable `viz.title`.

You MUST call exactly one tool per turn. Do not respond with free-form text.
"""

INTENT_TOOL_SCHEMA = {
    "name": "propose_intent",
    "description": "Submit the Intent JSON for the user's question. Call this only when you are confident in the entity, columns, filters, and scope.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["aggregate", "distribution", "raw"]},
            "metric": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
            "metric_column": {
                "type": "string",
                "description": "REQUIRED whenever metric != 'count'. The column the aggregate applies to (e.g. for 'avg monthly_income', set metric='avg' and metric_column='monthly_income').",
            },
            "entity": {"type": "string", "description": "Allow-listed table/view name."},
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "col": {"type": "string"},
                        "op":  {"type": "string", "enum": ["eq", "neq", "lt", "lte", "gt", "gte", "in", "like"]},
                        "val": {},
                        "note": {"type": "string"},
                    },
                    "required": ["col", "op", "val"],
                },
            },
            "group_by": {"type": "array", "items": {"type": "string"}},
            "order_by": {"type": "array", "items": {"type": "string"}},
            "limit":    {"type": "integer"},
            "viz": {
                "type": "object",
                "properties": {
                    "type":   {"type": "string", "description": "metric_card | bar | line | pie | donut | histogram | scatter | map | table"},
                    "x":      {"type": "string"},
                    "y":      {"type": "string"},
                    "series": {"type": "string"},
                    "title":  {"type": "string"},
                    "extra":  {"type": "object"},
                    "user_requested": {
                        "type": "boolean",
                        "description": "TRUE only when the user explicitly named this viz type in their question.",
                    },
                    "applies": {
                        "type": "boolean",
                        "description": "Whether this viz type fits the data this query will return. See VISUALISATION RULES in the system prompt.",
                    },
                    "applies_reason": {
                        "type": "string",
                        "description": "Required ONLY when applies=false. One polite sentence explaining why and what would work better.",
                    },
                },
            },
            "assumptions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["entity", "metric"],
    },
}

CLARIFY_TOOL_SCHEMA = {
    "name": "request_clarification",
    "description": "Ask the user a single targeted clarifying question when the original question is ambiguous, when a needed field/value is unclear, or when required scope is missing. Prefer offering 2–4 specific options.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "One concise question. No preamble.",
            },
            "context": {
                "type": "string",
                "description": "Which aspect is ambiguous — e.g. 'metric definition', 'geographic scope', 'enum value', 'time range', 'threshold'.",
            },
            "options": {
                "type": "array",
                "description": "0–6 suggested answers the user can click. Use empty list when the answer must be free-form (e.g. a number).",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":          {"type": "string", "description": "Short identifier returned to the backend."},
                        "label":       {"type": "string", "description": "Human-readable label."},
                        "description": {"type": "string", "description": "Optional one-line clarification."},
                    },
                    "required": ["id", "label"],
                },
            },
            "free_text_allowed": {
                "type": "boolean",
                "description": "If true, the user may also type a custom response instead of picking an option. Default true.",
            },
        },
        "required": ["question", "context"],
    },
}
