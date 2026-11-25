#!/usr/bin/env python3
"""
Mock OpenG2P Master Data Service — local simulation for the test stack.

Serves the two geo endpoints the Insight backend consumes:

    POST /geo/get_g2p_geo_levels
    POST /geo/get_g2p_geo_level_values

so the whole platform can be exercised on a laptop with **no dependency on a
real MDS deployment**.

Why it exists
-------------
The hierarchy is derived from the SAME geo tuples that `scripts/seed_data.py`
uses to generate registry rows (`FALLBACK_GEO`). That alignment matters: the
drill engine joins registry column values to MDS level values, so if the two
drifted apart every drill would return empty.

It also returns the newer fields (`pcode`, `display_name`, `boundary_uri`) that
were added to master-data-service, so the backend's pcode-preferring join path
is genuinely exercised locally rather than only the legacy name fallback.

Stdlib only — no pip install, so the image builds in seconds.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# (region, zone, woreda, kebele) — kept in lockstep with
# scripts/seed_data.py::FALLBACK_GEO.
GEO = [
    ("ET-AA", "AA-CENTRAL", "AA-W01", "AA-K001"),
    ("ET-AA", "AA-CENTRAL", "AA-W01", "AA-K002"),
    ("ET-AA", "AA-EAST", "AA-W02", "AA-K010"),
    ("ET-OR", "OR-EWELLEGA", "OR-W01", "OR-K001"),
    ("ET-OR", "OR-EWELLEGA", "OR-W01", "OR-K002"),
    ("ET-OR", "OR-EAST_SHEWA", "OR-W05", "OR-K050"),
    ("ET-AM", "AM-N_GONDAR", "AM-W01", "AM-K001"),
    ("ET-AM", "AM-S_WOLLO", "AM-W04", "AM-K040"),
    ("ET-TG", "TG-CENTRAL", "TG-W01", "TG-K001"),
    ("ET-SN", "SN-GURAGE", "SN-W02", "SN-K020"),
    ("ET-SN", "SN-WOLAYITA", "SN-W03", "SN-K031"),
    ("ET-SN", "SN-WOLAYITA", "SN-W03", "SN-K032"),
]

# Level chain, root first. Depth and naming are data here exactly as they are
# in the real service — the backend must never assume either.
LEVELS = [
    {"level_id": "region", "level_mnemonic": "region", "parent_level_id": None,
     "display_name": "Region"},
    {"level_id": "zone", "level_mnemonic": "zone", "parent_level_id": "region",
     "display_name": "Zone"},
    {"level_id": "woreda", "level_mnemonic": "woreda", "parent_level_id": "zone",
     "display_name": "Woreda"},
    {"level_id": "kebele", "level_mnemonic": "kebele", "parent_level_id": "woreda",
     "display_name": "Kebele"},
]

_PRETTY = {
    "ET-AA": "Addis Ababa", "ET-OR": "Oromia", "ET-AM": "Amhara",
    "ET-TG": "Tigray", "ET-SN": "SNNP",
}


def _label(code: str) -> str:
    """Human label. Real MDS supplies display_name; we synthesise something
    readable so the UI shows names rather than codes."""
    if code in _PRETTY:
        return _PRETTY[code]
    tail = code.split("-", 1)[-1]
    return tail.replace("_", " ").title()


def _build_values() -> list[dict]:
    """Flatten GEO into level values with path-style ids, mirroring the real
    service (`parent/child/grandchild`)."""
    seen: dict[str, dict] = {}
    for region, zone, woreda, kebele in GEO:
        chain = [
            ("region", region, None),
            ("zone", zone, region),
            ("woreda", woreda, f"{region}/{zone}"),
            ("kebele", kebele, f"{region}/{zone}/{woreda}"),
        ]
        for level_id, code, parent_path in chain:
            value_id = f"{parent_path}/{code}" if parent_path else code
            if value_id in seen:
                continue
            seen[value_id] = {
                "level_value_id": value_id,
                "level_id": level_id,
                "level_value_mnemonic": code,
                "parent_level_value_id": parent_path,
                # Newer master-data-service fields. `pcode` is deliberately the
                # SAME token the registry stores, so the backend's pcode join
                # path resolves — that is the join contract we want tested.
                "pcode": code,
                "pcode_source": "mock-test-stack",
                "display_name": _label(code),
                "display_name_i18n": None,
                "boundary_uri": None,
                "boundary_simplified_uri": None,
                "version": "mock-1",
                "valid_from": None,
                "valid_to": None,
            }
    return list(seen.values())


VALUES = _build_values()


def _envelope(payload) -> dict:
    return {
        "response_header": {
            "message_id": str(uuid.uuid4()),
            "message_ts": datetime.now(timezone.utc).isoformat(),
            "status": "succ",
        },
        "response_body": {"response_payload": payload},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code: int = 200) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            return self._send({"status": "ok", "levels": len(LEVELS), "values": len(VALUES)})
        self._send({"detail": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send({"detail": "bad json"}, 400)

        payload = ((body.get("request_body") or {}).get("request_payload")) or {}
        path = self.path.rstrip("/")

        if path.endswith("/geo/get_g2p_geo_levels"):
            parent = payload.get("parent_level_id")
            # Absent key => whole chain; explicit null => roots only. The real
            # service treats a missing parent as "all", which is what the
            # backend relies on to discover depth.
            if "parent_level_id" in payload and parent is None:
                out = [lv for lv in LEVELS if lv["parent_level_id"] is None]
            elif parent:
                out = [lv for lv in LEVELS if lv["parent_level_id"] == parent]
            else:
                out = LEVELS
            return self._send(_envelope(out))

        if path.endswith("/geo/get_g2p_geo_level_values"):
            level_id = payload.get("level_id")
            parent_value = payload.get("parent_level_value_id")
            out = [v for v in VALUES if v["level_id"] == level_id]
            if parent_value:
                out = [v for v in out if v["parent_level_value_id"] == parent_value]
            return self._send(_envelope(out))

        self._send({"detail": "not found"}, 404)

    def log_message(self, fmt, *args):  # quieter logs
        print("[mock-mds] " + (fmt % args))


if __name__ == "__main__":
    print(f"[mock-mds] serving {len(LEVELS)} levels / {len(VALUES)} values on :8080")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
