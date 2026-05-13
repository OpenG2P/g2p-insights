-- Create the additional databases the test stack needs.
-- The image initialises `registry` automatically from POSTGRES_DB; we add the
-- insight DB (for the FastAPI service's own schema) and a separate DB for
-- Superset's metadata.

CREATE DATABASE insight;
CREATE DATABASE superset;
