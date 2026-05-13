"""
OpenG2P Master Data Service client.

Wraps:
  POST /geo/get_g2p_geo_levels
  POST /geo/get_g2p_geo_level_values

Adds the G2PRequestHeader and unwraps response_body.response_payload. Caches
results in-memory with TTL.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class _TTLCache:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        ts, value = item
        if (time.time() - ts) > self._ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)


class MasterDataService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._cache = _TTLCache(_config.masterdata.cache_ttl_seconds)

    # ---------- public API ---------------------------------------------------

    async def get_levels(self, parent_level_id: Optional[str] = None) -> List[dict]:
        key = f"levels::{parent_level_id or ''}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload = {"parent_level_id": parent_level_id} if parent_level_id else {}
        body = await self._post(
            "/geo/get_g2p_geo_levels",
            request_payload=payload,
        )
        result = body.get("response_payload") or []
        self._cache.set(key, result)
        return result

    async def get_level_values(
        self, level_id: str, parent_level_value_id: Optional[str] = None
    ) -> List[dict]:
        key = f"values::{level_id}::{parent_level_value_id or ''}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload: dict = {"level_id": level_id}
        if parent_level_value_id:
            payload["parent_level_value_id"] = parent_level_value_id
        body = await self._post(
            "/geo/get_g2p_geo_level_values",
            request_payload=payload,
        )
        result = body.get("response_payload") or []
        self._cache.set(key, result)
        return result

    # ---------- internals ---------------------------------------------------

    async def _post(self, path: str, request_payload: dict) -> dict:
        url = _config.masterdata.url.rstrip("/") + path
        envelope = {
            "request_header": {
                "sender_app_mnemonic": _config.masterdata.sender_app_mnemonic,
                "sender_app_url": _config.openapi_common_api_prefix,
                "request_id": str(uuid.uuid4()),
                "request_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "request_body": {"request_payload": request_payload},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=envelope)
            r.raise_for_status()
            data = r.json()
        return data.get("response_body") or {}
