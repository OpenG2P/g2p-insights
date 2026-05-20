"""
Schema catalog service.

Loads catalog/schema.yaml at startup, exposes a typed view of it, and provides
relevance-based retrieval over table/column descriptions.

MVP retrieval is a keyword + TF-IDF fallback. Once the embedding model is in
place, switch `retrieve_relevant` to pgvector.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

import yaml
from openg2p_fastapi_common.service import BaseService
from pydantic import BaseModel, Field, field_validator

from ..config import Settings

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class ColumnSpec(BaseModel):
    name: str
    type: str
    pii: str = "safe"            # "safe" | "quasi" | "pii"
    description: Optional[str] = None
    unit: Optional[str] = None
    enum: Optional[List[str]] = None

    @field_validator("enum", mode="before")
    @classmethod
    def _coerce_enum_to_strings(cls, v):
        """YAML 1.1 (PyYAML's default) parses bare YES/NO/ON/OFF/True/False as
        booleans. Coerce every enum value to its string form so catalog
        authors don't get bitten by quoting subtleties."""
        if v is None:
            return v
        out: List[str] = []
        for item in v:
            if isinstance(item, bool):
                out.append("YES" if item else "NO")
            else:
                out.append(str(item))
        return out


class TableSpec(BaseModel):
    model_config = {"protected_namespaces": ()}  # allow `schema` attribute name

    name: str
    schema: str = "public"
    description: Optional[str] = None
    row_count_estimate: Optional[int] = None
    indexed: List[str] = Field(default_factory=list)
    columns: List[ColumnSpec] = Field(default_factory=list)
    refresh: Optional[str] = None
    notes: Optional[str] = None
    # OpenG2P registry concepts — set by `insight-onboard apply`.
    # The LLM uses `role` to know which table is the entity-level register
    # (use for "how many individuals?" counts) vs a supporting child table
    # (use only when asking about the aspect that table holds).
    # Values: "register" | "programme_register" | "table" | None (legacy catalogs).
    role: Optional[str] = None
    # For role=table: which register this table hangs off (the safe-view name).
    parent_register: Optional[str] = None

    def safe_columns(self) -> List[ColumnSpec]:
        return [c for c in self.columns if c.pii != "pii"]


class DeploymentSpec(BaseModel):
    country_code: str = ""
    default_currency: str = ""
    k_anonymity: int = 5
    master_data_service_url: Optional[str] = None


class GeoColumn(BaseModel):
    column: str
    level_mnemonic: str


class Catalog(BaseModel):
    deployment: DeploymentSpec = DeploymentSpec()
    tables: List[TableSpec] = Field(default_factory=list)
    pii_columns_in_source: List[dict] = Field(default_factory=list)
    safe_views: List[str] = Field(default_factory=list)
    geo_columns: List[GeoColumn] = Field(default_factory=list)

    def safe_table_names(self) -> List[str]:
        """Fail-secure allow-list of entities the AI agent may query.

        Returns ONLY the names explicitly listed under `safe_views` in the
        catalog. If `safe_views` is empty, returns an empty list — the planner
        will then refuse every query, which is the correct behaviour for an
        unconfigured deployment. We do NOT fall back to "any table without a
        PII column" — a forgotten classification on a single column would
        otherwise expose PII.
        """
        return sorted(set(self.safe_views))


class SchemaService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self.catalog: Catalog = Catalog()
        self.reload()

    def reload(self) -> None:
        path = Path(_config.catalog_path) / "schema.yaml"
        if not path.exists():
            _logger.warning("schema.yaml not found at %s — running with empty catalog", path)
            self.catalog = Catalog()
            return
        data = yaml.safe_load(path.read_text()) or {}
        self.catalog = Catalog.model_validate(data)
        _logger.info("Loaded catalog with %d tables", len(self.catalog.tables))

    # Catalogs at or below this size are sent to the LLM in full — keyword
    # retrieval only earns its keep on large schemas. A typical registry has
    # a couple of registers + a handful of supporting tables (NSR has 12
    # views), all of which fit comfortably in context. Dropping any table
    # risks hiding the one the question needs — we saw the `individuals`
    # register get cut purely by alphabetical tie-breaking under the old
    # top-k scorer, forcing the LLM to guess the table name.
    _SEND_ALL_THRESHOLD = 25

    async def retrieve_relevant(self, query: str, top_k: int = 8) -> List[TableSpec]:
        """
        Return the TableSpecs to put in the LLM prompt for this query.

        Strategy:
          1. Small catalog (≤ _SEND_ALL_THRESHOLD): send everything. Retrieval
             can only hurt here — there's no token budget pressure and any
             omission risks hiding the needed table.
          2. Large catalog: ALWAYS include every register / programme_register
             (the backbone of entity questions — never drop one), then fill
             the remaining slots by keyword-overlap score, then pad with
             whatever's left.

        Keyword scoring is the MVP retriever; replace with pgvector +
        embeddings in v2.
        """
        all_tables = self.catalog.tables
        if not all_tables:
            return []

        # 1. Small catalog → send all. `top_k` is treated as a floor here so
        #    callers asking for more than the threshold still get everything.
        if len(all_tables) <= max(top_k, self._SEND_ALL_THRESHOLD):
            return list(all_tables)

        # 2. Large catalog. Registers always make the cut.
        registers = [
            t for t in all_tables
            if (t.role or "") in ("register", "programme_register")
        ]
        register_names = {t.name for t in registers}
        others = [t for t in all_tables if t.name not in register_names]

        tokens = self._tokenize(query)
        scored: List[tuple[int, TableSpec]] = []
        for table in others:
            haystack_tokens = self._tokenize(
                " ".join(
                    [table.name, table.description or "", *(c.name for c in table.columns)]
                )
            )
            overlap = len(tokens & haystack_tokens)
            if overlap:
                scored.append((overlap, table))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Registers first (never dropped), then keyword-ranked others, then
        # pad with any remaining tables until we hit top_k.
        result: List[TableSpec] = list(registers)
        for _, t in scored:
            if len(result) >= top_k:
                break
            result.append(t)
        if len(result) < top_k:
            chosen = {t.name for t in result}
            for t in others:
                if t.name in chosen:
                    continue
                result.append(t)
                if len(result) >= top_k:
                    break
        return result

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(t for t in re.split(r"\W+", text.lower()) if len(t) > 2)
