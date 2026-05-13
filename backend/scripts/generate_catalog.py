#!/usr/bin/env python3
"""
Generate a draft schema.yaml from a registry extension's SQLAlchemy models.

Usage:
    python generate_catalog.py \
        --package openg2p_registry_farmer_extension.register_domain.models \
        --output catalog/schema.yaml

The generated file is a starting point. After generation, edit it manually to:
  - Adjust PII classifications (safe / quasi / pii)
  - Add column descriptions
  - Add row count estimates
  - Add unit annotations and notes

Re-running the script will OVERWRITE the file — keep your edits in version control.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
import sys
from pathlib import Path

import yaml
from sqlalchemy import inspect

# Heuristic PII patterns — used only for the initial draft. Operator must review.
_PII_PATTERNS = {
    "pii": ("name", "phone", "email", "address", "fayda", "aadhaar", "national_id", "ssn", "dob", "birth_date"),
    "quasi": ("age", "gender", "marital", "ethnicity", "religion", "language"),
}


def _classify_column(name: str) -> str:
    lower = name.lower()
    for cls, patterns in _PII_PATTERNS.items():
        if any(p in lower for p in patterns):
            return cls
    return "safe"


def _import_all(package_name: str) -> None:
    pkg = importlib.import_module(package_name)
    if not hasattr(pkg, "__path__"):
        return
    for mod_info in pkgutil.walk_packages(pkg.__path__, prefix=f"{package_name}."):
        importlib.import_module(mod_info.name)


def _model_to_dict(mapper) -> dict:
    table = mapper.local_table
    cols = []
    for col in table.columns:
        cols.append(
            {
                "name": col.name,
                "type": str(col.type),
                "pii": _classify_column(col.name),
                "nullable": bool(col.nullable),
            }
        )
    return {
        "name": table.name,
        "schema": table.schema or "public",
        "description": (mapper.class_.__doc__ or "").strip().split("\n")[0] if mapper.class_.__doc__ else "",
        "indexed": [ix.name for ix in table.indexes],
        "columns": cols,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate insight schema.yaml from SQLAlchemy models.")
    parser.add_argument("--package", required=True, help="Python dotted path to the models package.")
    parser.add_argument("--output", default="catalog/schema.yaml")
    parser.add_argument("--country-code", default="")
    parser.add_argument("--default-currency", default="")
    parser.add_argument("--k-anonymity", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    _import_all(args.package)

    from sqlalchemy.orm import DeclarativeMeta  # noqa: F401

    # Walk the registry: any class whose __table__ exists.
    tables: list[dict] = []
    seen: set[str] = set()
    for mod_name in list(sys.modules):
        if not mod_name.startswith(args.package):
            continue
        mod = sys.modules[mod_name]
        for attr in vars(mod).values():
            if not hasattr(attr, "__table__"):
                continue
            try:
                mapper = inspect(attr)
            except Exception:  # noqa: BLE001
                continue
            tn = mapper.local_table.fullname
            if tn in seen:
                continue
            seen.add(tn)
            tables.append(_model_to_dict(mapper))

    tables.sort(key=lambda t: t["name"])

    catalog = {
        "deployment": {
            "country_code": args.country_code,
            "default_currency": args.default_currency,
            "k_anonymity": args.k_anonymity,
        },
        "tables": tables,
        "pii_columns_in_source": [
            {"table": t["name"], "column": c["name"]}
            for t in tables
            for c in t["columns"]
            if c["pii"] == "pii"
        ],
        "safe_views": [],
        "geo_columns": [],
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# DRAFT — review every column's `pii` classification before deploying.\n"
        + yaml.safe_dump(catalog, sort_keys=False)
    )
    print(f"Wrote {len(tables)} tables to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
