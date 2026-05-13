#!/usr/bin/env python3
"""
Probe the configured OpenG2P Master Data Service and snapshot Ethiopia's geo
hierarchy locally so seed_data.py (and the running backend) can rely on it.

The public MDS OpenAPI exposes:
  POST /geo/get_g2p_geo_levels         — list levels
  POST /geo/get_g2p_geo_level_values   — list values at a level (optionally
                                         filtered by parent value)

There is no documented write endpoint in v2.0.0, so this script does NOT
attempt to push data INTO the MDS. The MDS at the configured URL is expected
to already contain Ethiopia's hierarchy. This script:

  1. Pings the MDS.
  2. Walks the hierarchy down to the kebele level.
  3. Writes a JSON snapshot to test-stack/postgres/cache/ethiopia_geo.json.
     The snapshot is then used by seed_data.py as the seeded geo set —
     keeping registry rows aligned with what the running insight backend
     resolves through /geo endpoints.

Usage:
    python seed_master_data.py [--mds <url>] [--out <path>] [--country ET]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


def post(base: str, path: str, payload: dict) -> dict:
    envelope = {
        "request_header": {
            "sender_app_mnemonic": "g2p-insight-seed",
            "sender_app_url": "/seed",
            "request_id": str(uuid.uuid4()),
            "request_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "request_body": {"request_payload": payload},
    }
    r = httpx.post(base.rstrip("/") + path, json=envelope, timeout=20)
    r.raise_for_status()
    return r.json()


def walk_geo(base: str) -> dict:
    """Return a nested dict capturing region → zone → woreda → kebele."""
    levels = post(base, "/geo/get_g2p_geo_levels", {})["response_body"]["response_payload"]
    by_mnemonic = {lv["level_mnemonic"]: lv["level_id"] for lv in levels}
    needed = ["region", "zone", "woreda", "kebele"]
    missing = [m for m in needed if m not in by_mnemonic]
    if missing:
        raise RuntimeError(
            f"MDS at {base} is missing levels {missing!r}. Found: {sorted(by_mnemonic)}"
        )

    def values(level_id: str, parent_id: str | None) -> list[dict]:
        payload = {"level_id": level_id}
        if parent_id:
            payload["parent_level_value_id"] = parent_id
        return post(base, "/geo/get_g2p_geo_level_values", payload)["response_body"]["response_payload"]

    tree: dict = {"levels": levels, "regions": []}
    for region in values(by_mnemonic["region"], None):
        r_entry: dict = {**region, "zones": []}
        for zone in values(by_mnemonic["zone"], region["level_value_id"]):
            z_entry: dict = {**zone, "woredas": []}
            for woreda in values(by_mnemonic["woreda"], zone["level_value_id"]):
                w_entry: dict = {**woreda, "kebeles": []}
                for kebele in values(by_mnemonic["kebele"], woreda["level_value_id"]):
                    w_entry["kebeles"].append(kebele)
                z_entry["woredas"].append(w_entry)
            r_entry["zones"].append(z_entry)
        tree["regions"].append(r_entry)
    return tree


def summarise(tree: dict) -> dict:
    return {
        "levels": [lv["level_mnemonic"] for lv in tree["levels"]],
        "regions": len(tree["regions"]),
        "zones":   sum(len(r["zones"]) for r in tree["regions"]),
        "woredas": sum(len(w) for r in tree["regions"] for w in (z["woredas"] for z in r["zones"])),
        "kebeles": sum(
            len(w["kebeles"])
            for r in tree["regions"]
            for z in r["zones"]
            for w in z["woredas"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mds",
        default=os.environ.get("MASTERDATA_URL", "https://master-data.trial.openg2p.org"),
    )
    parser.add_argument("--out", default="postgres/cache/ethiopia_geo.json")
    parser.add_argument("--country", default="ET")  # informational
    ns = parser.parse_args()

    print(f"[seed-md] MDS={ns.mds}  country={ns.country}")

    try:
        tree = walk_geo(ns.mds)
    except Exception as exc:  # noqa: BLE001
        print(f"[seed-md] ERROR walking MDS: {exc}", file=sys.stderr)
        print("[seed-md] No snapshot written. seed_data.py will fall back to its built-in sample.", file=sys.stderr)
        return 2

    summary = summarise(tree)
    out = Path(ns.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tree, ensure_ascii=False, indent=2))

    print(f"[seed-md] Wrote {out} — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
