#!/usr/bin/env python3
"""
Generate ~10k synthetic individuals (+ households, shocks, programs, housing)
into the registry database for end-to-end testing.

Uses a fixed seed so re-runs are reproducible. After seeding, refreshes the
materialized views so dashboard / analytics queries are immediately fast.

Usage:
    python seed_data.py [--households 2500] [--db ...]

Defaults to roughly:
    - 2,500 households (avg 4 members ⇒ ~10k individuals)
    - 20% have at least one shock event
    - 35% are enrolled in at least one program (some in two ⇒ overlap analysis)

Geo codes are read from the Master Data Service if it's reachable; otherwise
a small hard-coded Ethiopia sample (Addis, Oromia, Amhara, Tigray, SNNP) is
used so the script never blocks tests.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
import psycopg

SEED = 4242
random.seed(SEED)


# -----------------------------------------------------------------------------
# Geo — pulled live from MDS when possible
# -----------------------------------------------------------------------------
FALLBACK_GEO = [
    # (region, zone, woreda, kebele) — small synthetic sample
    ("ET-AA", "AA-CENTRAL", "AA-W01", "AA-K001"),
    ("ET-AA", "AA-CENTRAL", "AA-W01", "AA-K002"),
    ("ET-AA", "AA-EAST",    "AA-W02", "AA-K010"),
    ("ET-OR", "OR-EWELLEGA", "OR-W01", "OR-K001"),
    ("ET-OR", "OR-EWELLEGA", "OR-W01", "OR-K002"),
    ("ET-OR", "OR-EAST_SHEWA", "OR-W05", "OR-K050"),
    ("ET-AM", "AM-N_GONDAR", "AM-W01", "AM-K001"),
    ("ET-AM", "AM-S_WOLLO",  "AM-W04", "AM-K040"),
    ("ET-TG", "TG-CENTRAL",  "TG-W01", "TG-K001"),
    ("ET-SN", "SN-GURAGE",   "SN-W02", "SN-K020"),
    ("ET-SN", "SN-WOLAYITA", "SN-W03", "SN-K031"),
    ("ET-SN", "SN-WOLAYITA", "SN-W03", "SN-K032"),
]


def fetch_geo(mds_url: str | None) -> list[tuple[str, str, str, str]]:
    """Try MDS for region→zone→woreda→kebele combos; fall back on error."""
    if not mds_url:
        return FALLBACK_GEO
    try:
        return _fetch_geo_from_mds(mds_url)
    except Exception as exc:  # noqa: BLE001
        print(f"[seed] MDS unreachable ({exc}); using fallback geo set", file=sys.stderr)
        return FALLBACK_GEO


def _fetch_geo_from_mds(mds_url: str) -> list[tuple[str, str, str, str]]:
    """Walk the geo hierarchy in the MDS and assemble (region, zone, woreda, kebele) tuples."""
    base = mds_url.rstrip("/")
    levels = _post(base + "/geo/get_g2p_geo_levels", {})["response_body"]["response_payload"]
    by_mnemonic = {lv["level_mnemonic"]: lv["level_id"] for lv in levels}
    needed = ["region", "zone", "woreda", "kebele"]
    if not all(m in by_mnemonic for m in needed):
        raise RuntimeError(f"MDS missing levels — got {sorted(by_mnemonic)}; need {needed}")

    out: list[tuple[str, str, str, str]] = []

    def values(level_id: str, parent_id: str | None) -> list[dict]:
        body = _post(
            base + "/geo/get_g2p_geo_level_values",
            {"level_id": level_id, **({"parent_level_value_id": parent_id} if parent_id else {})},
        )
        return body["response_body"]["response_payload"]

    for region in values(by_mnemonic["region"], None):
        for zone in values(by_mnemonic["zone"], region["level_value_id"]):
            for woreda in values(by_mnemonic["woreda"], zone["level_value_id"]):
                kebeles = values(by_mnemonic["kebele"], woreda["level_value_id"])[:6]
                for kebele in kebeles:
                    out.append((
                        region["level_value_mnemonic"],
                        zone["level_value_mnemonic"],
                        woreda["level_value_mnemonic"],
                        kebele["level_value_mnemonic"],
                    ))
    return out or FALLBACK_GEO


def _post(url: str, request_payload: dict) -> dict:
    envelope = {
        "request_header": {
            "sender_app_mnemonic": "g2p-insight-seed",
            "sender_app_url": "/seed",
            "request_id": str(uuid.uuid4()),
            "request_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "request_body": {"request_payload": request_payload},
    }
    r = httpx.post(url, json=envelope, timeout=15)
    r.raise_for_status()
    return r.json()


# -----------------------------------------------------------------------------
# Distribution helpers — designed to make analytical queries non-trivial.
# -----------------------------------------------------------------------------
GENDERS    = ["M", "F", "F", "F", "O"]  # ~slightly female-skewed
LIVELIHOOD = ["CROP_FARMING", "LIVESTOCK", "PASTORALIST", "MIXED", "WAGE_LABOR", "BUSINESS", "OTHER"]
EMPLOY     = ["EMPLOYED", "SELF_EMPLOYED", "UNEMPLOYED", "STUDENT", "OTHER"]
HEADSHIP   = ["MALE_HEADED", "FEMALE_HEADED", "ELDERLY_HEADED", "CHILD_HEADED", "DISABLED_HEADED"]
DISABILITY = ["NO", "NO", "NO", "NO", "YES", "UNKNOWN"]
DISPLACE   = [None, None, None, None, "IDP", "REFUGEE", "RETURNEE", "HOST_COMMUNITY"]
PASTORAL   = ["SETTLED", "SETTLED", "SETTLED", "SEMI_PASTORALIST", "PASTORALIST"]
PROGRAMS   = ["PSNP", "CBHI", "PSSN", "AGP", "RDP"]

AGE_BUCKETS = [(0, 5, "0-5"), (6, 17, "6-17"), (18, 35, "18-35"), (36, 55, "36-55"), (56, 90, "55+")]


def pick_age() -> tuple[int, str]:
    lo, hi, label = random.choice(AGE_BUCKETS)
    return random.randint(lo, hi), label


def fake_name(rng: random.Random) -> str:
    first = rng.choice(["Almaz", "Bekele", "Chala", "Dawit", "Eden", "Fatuma", "Girma", "Hanna", "Iyob", "Jemila"])
    last = rng.choice(["Tesfaye", "Gebre", "Asfaw", "Hailu", "Bekele", "Tadesse", "Mohammed", "Wolde", "Yohannes"])
    return f"{first} {last}"


# -----------------------------------------------------------------------------
# Inserts
# -----------------------------------------------------------------------------
@dataclass
class Args:
    households: int
    db_url: str
    mds_url: str | None


def main() -> int:
    args = parse_args()
    print(f"[seed] households={args.households}  db={args.db_url}  mds={args.mds_url or '<fallback>'}")

    geo = fetch_geo(args.mds_url)
    print(f"[seed] geo combinations: {len(geo)}")

    rng = random.Random(SEED)

    with psycopg.connect(args.db_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE g2p_register_individual_shocks, "
                        "g2p_register_individual_programs, "
                        "g2p_register_household_housing, "
                        "g2p_register_individuals, "
                        "g2p_register_households RESTART IDENTITY CASCADE")

            hh_rows: list[tuple] = []
            for _ in range(args.households):
                region, zone, woreda, kebele = rng.choice(geo)
                size = max(1, int(rng.gauss(4.5, 1.6)))
                female_headed = rng.random() < 0.32
                pmt = round(rng.gauss(35, 12), 2)
                income = round(max(0, rng.gauss(2500, 1500)), 2)
                hh_rows.append((
                    f"RID-H-{uuid.uuid4().hex[:10]}",
                    rng.choice(HEADSHIP) if not female_headed else "FEMALE_HEADED",
                    size, female_headed,
                    rng.random() < 0.18,            # has_disability_member
                    rng.random() < 0.08,            # has_idp_member
                    region, zone, woreda, kebele,
                    pmt, income,
                    rng.randint(0, 60),
                    rng.choice(["FAYDA", "MANUAL", "UNVERIFIED"]),
                    datetime.now(timezone.utc) - timedelta(days=rng.randint(0, 365)),
                    rng.random() < 0.65,
                    f"{rng.randint(1, 999)} Local Rd",
                    "+251" + "".join(str(rng.randint(0, 9)) for _ in range(9)),
                    fake_name(rng),
                ))

            cur.executemany(
                """INSERT INTO g2p_register_households
                   (rid, headship_type, household_size, female_headed, has_disability_member,
                    has_idp_member, region_code, zone_code, woreda_code, kebele_code,
                    pmt_score, monthly_income, coping_strategies_index, verification_method,
                    registered_at, fayda_verified, address_line, contact_phone, head_full_name)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                hh_rows,
            )
            print(f"[seed] inserted {len(hh_rows)} households")

            # housing — one row per household
            cur.execute("SELECT id, region_code FROM g2p_register_households")
            household_ids: list[tuple[int, str]] = list(cur.fetchall())
            housing_rows = []
            for hh_id, _region in household_ids:
                housing_rows.append((
                    hh_id,
                    rng.choice(["PERMANENT", "SEMI", "TEMPORARY"]),
                    rng.choice(["OWNED", "RENTED"]),
                    rng.randint(1, 6),
                    rng.choice(["IRON_SHEET", "THATCH", "CONCRETE", "OTHER"]),
                    rng.choice(["MUD", "BRICK", "WOOD", "STONE"]),
                    rng.choice(["EARTH", "WOOD", "CEMENT", "TILE"]),
                    rng.choice(["PIPED", "BOREHOLE", "RIVER", "PROTECTED_SPRING"]),
                    rng.random() < 0.55,
                    rng.choice(["PIT", "VIP", "FLUSH", "OPEN"]),
                ))
            cur.executemany(
                """INSERT INTO g2p_register_household_housing
                   (household_id, dwelling_type, tenure_status, rooms, roof_material,
                    wall_material, floor_material, water_source, electricity, sanitation_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                housing_rows,
            )

            # individuals — one head + (size-1) members per household
            ind_rows = []
            for hh_id, _ in household_ids:
                cur.execute(
                    "SELECT household_size, region_code, zone_code, woreda_code, kebele_code, "
                    "pmt_score, monthly_income FROM g2p_register_households WHERE id = %s",
                    (hh_id,),
                )
                size, region, zone, woreda, kebele, hh_pmt, hh_income = cur.fetchone()
                for member_idx in range(size):
                    is_head = member_idx == 0
                    age, bracket = pick_age()
                    if is_head:
                        age = max(age, 18)
                        bracket = next(b for lo, hi, b in AGE_BUCKETS if lo <= age <= hi)
                    ind_rows.append((
                        hh_id,
                        f"RID-I-{uuid.uuid4().hex[:10]}",
                        rng.choice(["PENDING", "VERIFIED", "REJECTED"]),
                        fake_name(rng),
                        "+251" + "".join(str(rng.randint(0, 9)) for _ in range(9)) if is_head else None,
                        None,
                        f"{rng.randint(1, 999)} {kebele} Rd" if is_head else None,
                        None,
                        round(rng.uniform(3.0, 14.5), 6),
                        round(rng.uniform(33.0, 47.0), 6),
                        region, zone, woreda, kebele, kebele[-3:],
                        rng.choice(GENDERS),
                        age, bracket,
                        "OTHER" if is_head else rng.choice(["SPOUSE", "CHILD", "PARENT", "OTHER"]),
                        rng.choice(["USUAL_MEMBER", "USUAL_MEMBER", "USUAL_MEMBER", "TEMPORARY", "ABSENT"]),
                        rng.choice(["CITIZEN", "CITIZEN", "CITIZEN", "REFUGEE", "IDP"]),
                        is_head,
                        rng.choice(LIVELIHOOD) if age >= 15 else None,
                        rng.choice(LIVELIHOOD) if (age >= 15 and rng.random() < 0.35) else None,
                        rng.choice(EMPLOY) if age >= 15 else "STUDENT",
                        rng.randint(0, 60),
                        rng.random() < 0.55,                                  # land_access
                        round(rng.uniform(0, 5), 2) if rng.random() < 0.55 else None,
                        rng.choice(DISABILITY),
                        rng.choice(["MILD", "MODERATE", "SEVERE"]) if rng.random() < 0.15 else None,
                        rng.random() < 0.04,
                        rng.random() < 0.09,
                        rng.choice(DISPLACE),
                        rng.choice(PASTORAL),
                        rng.random() < 0.05,
                        rng.random() < 0.05 and (age >= 14 and age <= 49),    # plw_status
                        round(max(0, rng.gauss(hh_income / max(1, size), 800)), 2) if is_head else None,
                        round(max(0, rng.gauss(hh_pmt, 8)), 2),
                        uuid.uuid4().hex[:12] if rng.random() < 0.65 else None,
                        rng.random() < 0.55,
                        rng.choice(["FAYDA", "MANUAL", "UNVERIFIED"]),
                        datetime.now(timezone.utc) - timedelta(days=rng.randint(0, 365)),
                    ))

            # Bulk insert in chunks to keep memory bounded.
            for chunk_start in range(0, len(ind_rows), 5000):
                chunk = ind_rows[chunk_start : chunk_start + 5000]
                cur.executemany(
                    """INSERT INTO g2p_register_individuals
                       (household_id, rid, rid_status, full_name, primary_phone, alternate_phone,
                        address_descriptor, contact_person_name, gps_latitude, gps_longitude,
                        region_code, zone_code, woreda_code, kebele_code, locality_ea_code,
                        gender, estimated_age, age_bracket, relationship_to_head, residency_status,
                        citizenship_category, is_head, primary_livelihood, secondary_livelihood,
                        employment_status, coping_strategies_index, land_access, land_size,
                        disability_status, disability_severity, orphanhood_flag, chronic_illness_flag,
                        displacement_status, pastoralist_classification, high_mobility_indicator,
                        plw_status, monthly_income, pmt_score, fayda_token, fayda_verified,
                        verification_method, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    chunk,
                )
            print(f"[seed] inserted {len(ind_rows)} individuals")

            # programs — ~35% enrolled in something; ~6% in two
            cur.execute("SELECT id FROM g2p_register_individuals")
            ind_ids = [r[0] for r in cur.fetchall()]
            prog_rows = []
            for iid in ind_ids:
                if rng.random() < 0.35:
                    prog_rows.append((
                        iid, rng.choice(PROGRAMS),
                        date.today() - timedelta(days=rng.randint(30, 1500)),
                        rng.random() < 0.20,
                        rng.choice(["ACTIVE", "EXITED", "SUSPENDED"]),
                    ))
                    if rng.random() < 0.18:    # ~6% overall → double-dipping
                        prog_rows.append((
                            iid, rng.choice(PROGRAMS),
                            date.today() - timedelta(days=rng.randint(30, 900)),
                            rng.random() < 0.20,
                            "ACTIVE",
                        ))
            cur.executemany(
                """INSERT INTO g2p_register_individual_programs
                   (individual_id, program_code, enrolled_at, is_legacy, status)
                   VALUES (%s,%s,%s,%s,%s)""",
                prog_rows,
            )
            print(f"[seed] inserted {len(prog_rows)} program enrolments")

            # shocks — ~20% affected
            shock_rows = []
            for iid in ind_ids:
                if rng.random() < 0.20:
                    csi_at = rng.randint(0, 60)
                    shock_rows.append((
                        iid,
                        rng.choice(["DROUGHT", "FLOOD", "CONFLICT", "ILLNESS", "OTHER"]),
                        rng.choice(["MILD", "MODERATE", "SEVERE"]),
                        date.today() - timedelta(days=rng.randint(30, 730)),
                        csi_at,
                        max(0, csi_at - rng.randint(0, 15)),
                        None,
                    ))
            cur.executemany(
                """INSERT INTO g2p_register_individual_shocks
                   (individual_id, shock_type, severity, occurred_at, csi_at_shock, csi_after, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                shock_rows,
            )
            print(f"[seed] inserted {len(shock_rows)} shock events")

        conn.commit()

        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW mv_individuals_summary")
            cur.execute("REFRESH MATERIALIZED VIEW mv_district_aggregates")
        conn.commit()
        print("[seed] refreshed materialized views")

    return 0


def parse_args() -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--households", type=int, default=2500)
    parser.add_argument(
        "--db", default=os.environ.get(
            "REGISTRY_DB_URL",
            "postgresql://postgres:postgres@localhost:5433/registry",
        ),
    )
    parser.add_argument(
        "--mds", default=os.environ.get(
            "MASTERDATA_URL",
            "https://master-data.trial.openg2p.org",
        ),
    )
    ns = parser.parse_args()
    return Args(households=ns.households, db_url=ns.db, mds_url=ns.mds)


if __name__ == "__main__":
    sys.exit(main())
