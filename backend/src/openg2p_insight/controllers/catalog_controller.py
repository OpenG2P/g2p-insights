"""Schema catalog read/reload endpoints. Editing UI is post-MVP."""

from __future__ import annotations

from openg2p_fastapi_common.controller import BaseController

from ..services.schema_service import SchemaService


class CatalogController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["catalog"]

        self._schema = SchemaService.get_component()

        @self.router.get("/catalog")
        async def get_catalog() -> dict:
            return self._schema.catalog.model_dump()

        @self.router.post("/catalog/reload")
        async def reload() -> dict:
            self._schema.reload()
            return {"status": "reloaded", "tables": len(self._schema.catalog.tables)}
