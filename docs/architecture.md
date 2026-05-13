# Architecture deep-dive

See the top-level [README.md](../README.md) for the canonical architecture
diagram and per-flow specifications. This document is reserved for deeper
discussions that don't fit there:

- Cost-gate tuning over time
- Embedding model evaluation (after pgvector wiring)
- Strategy for switching to a local LLM (Qwen / Llama via vLLM)
- Cube.dev semantic layer integration sketch
- OpenSearch as a future analytical backend
