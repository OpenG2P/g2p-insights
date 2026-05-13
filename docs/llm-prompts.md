# LLM prompts

The system prompts live in `backend/src/openg2p_insight/prompts/`. There is one
file per task:

- `nl_to_intent.py` — NL question → Intent JSON for ad-hoc queries (Flow A).
- (Forthcoming) `chart_recommendation.py` — bias the LLM toward chart-friendly
  intents when authoring Superset charts (Flow B).
- (Forthcoming) `analysis_routing.py` — pick the right analysis_function for
  expert questions (Flow C, optional).

## Editing rules

- Keep the constraints section short and absolute.
- The tool schema (`INTENT_TOOL_SCHEMA`) is the contract. Backward-incompatible
  changes must be versioned.
- Every release we evaluate prompts against a held-out test set
  (`backend/tests/test_llm_intents.py`, forthcoming).
