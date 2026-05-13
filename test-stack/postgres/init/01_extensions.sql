-- Run on the registry DB (the default DB the image initialises against).
\connect registry
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
