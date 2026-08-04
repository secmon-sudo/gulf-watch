"""Fill the database with plausible synthetic traffic.

Purpose: prove the pipeline end to end before you have OpenSky credentials, and
give you something to point the dashboard at on day one. It also lets you test
the alerting logic deliberately -- the scenario below suspends two carriers and
degrades a third.

    python -m src.demo_seed          # 120 days of synthetic history
    python -m src.demo_seed --wipe   # start over

NEVER publish demo data as real. publish.py stamps `"demo": true` into
health.json when this has been run; delete data/gulfwatch.db before going live.
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone

from . import backfill, config, db, metrics, suspensions

random.seed(7)

# (carrier, dep, arr, weekly frequency before escalation)
NETWORK = [
    ("QTR", "OTHH", "OMDB", 21), ("QTR", "OTHH", "OBBI", 14),
    ("QTR", "OTHH", "OKBK", 14), ("QTR", "OTHH", "OERK", 14),
    ("ETD", "OMAA", "OTHH", 7),  ("ETD", "OMAA", "OBBI", 7),
    ("FDB", "OMDB", "OBBI", 14), ("FDB", "OMDB", "OKBK", 14),
    ("FDB", "OMDB", "ORBI", 7),
    ("PGT", "OMDB", "OBBI", 4),  ("PGT", "OTHH", "OMDB", 3),
    ("SVA", "OERK", "OMDB", 14), ("SVA", "OEJN", "OMDB", 7),
    ("KNE", "OERK", "OMDB", 10), ("KNE", "OEJN", "OBBI", 6),
    ("GFA", "OBBI", "OMDB", 14), ("GFA", "OBBI", "OKBK", 7),
    ("OMA", "OOMS", "OMDB", 14), ("OMA", "OOMS", "OTHH", 7),
    ("KAC", "OKBK", "OMDB", 10), ("KAC", "OKBK", "OBBI", 7),
    ("RJA", "OJAI", "OMDB", 7),  ("RJA", "OJAI", "OTHH", 5),
    ("MEA", "OLBA", "OMDB", 7),  ("MEA", "OLBA", "OBBI", 4),
    ("ABY", "OMSJ", "OBBI", 7),  ("ABY", "OMSJ", "OKBK", 7),
    ("ABG", "OMAA", "OMDB", 3),
    ("MSR", "OMDB", "OTHH", 5),  ("RBG", "OMSJ", "OMDB", 3),
    ("DAH", "OMDB", "OTHH", 2),  ("RAM", "OMDB", "OTHH", 3),
    ("BAW", "OMDB", "OTHH", 7),  ("BAW", "OBBI", "OMDB", 4),
    ("FIN", "OMDB", "OTHH", 5),  ("IBE", "OMDB", "OTHH", 3),
    # control group
    ("UAE", "OMDB", "OTHH", 21), ("UAE", "OMDB", "OBBI", 21),
    ("THY", "OMDB", "OTHH", 14), ("DLH", "OMDB", "OTHH", 10),
    ("AFR", "OMDB", "OTHH", 7),  ("KLM", "OMDB", "OTHH", 7),
]

# Escalation scenario, applied to the most recent N days.
ESCALATION_DAYS = 21
SCENARIO = {
    "MEA": 0.0,    # Beirut operations stopped
    "RJA": 0.0,    # Amman operations stopped
    "KAC": 0.25,   # heavily reduced
    "GFA": 0.55,   # reduced
    "FIN": 0.40,   # European carrier pulling back
    "BAW": 0.50,
}


def seed(days: int = 120, wipe: bool = False) -> None:
    conn = db.connect()
    if wipe:
        # suspension/evidence must go too. Leaving stale active events behind
        # makes the replay below resume them against freshly seeded traffic,
        # which back-dates resumed_on to before started_on.
        for table in ("flight", "daily_route", "baseline", "coverage",
                      "fir_transit", "run_log", "suspension", "evidence"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    today = datetime.now(tz=timezone.utc).date()
    rows = []
    icao_pool = {c: f"{i:06x}" for i, c in enumerate({n[0] for n in NETWORK}, start=0x400000)}

    for offset in range(days, -1, -1):
        day = today - timedelta(days=offset)
        in_escalation = offset < ESCALATION_DAYS
        for carrier, dep, arr, weekly in NETWORK:
            rate = weekly / 7.0
            if in_escalation and carrier in SCENARIO:
                rate *= SCENARIO[carrier]
            n = int(rate) + (1 if random.random() < (rate % 1) else 0)
            for k in range(n):
                ts = int(datetime.combine(
                    day, datetime.min.time(), tzinfo=timezone.utc
                ).timestamp()) + 3600 * (5 + k * 2) + random.randint(0, 1800)
                fn = random.randint(100, 999)
                rows.append({
                    "icao24": f"{int(icao_pool[carrier], 16) + k:06x}",
                    "first_seen": ts,
                    "last_seen": ts + 7200,
                    "callsign": f"{carrier}{fn}",
                    "carrier": carrier,
                    "flight_number": fn,
                    "dep_icao": dep,
                    "arr_icao": arr,
                    "is_freight": 0,
                    "dep_date": day.isoformat(),
                    "source": "demo",
                    "ingested_at": ts,
                })

    db.upsert_flights(conn, rows)
    metrics.rebuild_daily(conn)

    base_end = (today - timedelta(days=ESCALATION_DAYS + 1)).isoformat()
    base_start = (today - timedelta(days=days)).isoformat()
    backfill.freeze(base_start, base_end)

    for offset in range(0, 30):
        metrics.score_coverage(conn, today - timedelta(days=offset))

    # Synthetic FIR transits: near-total avoidance of the CZIB airspace, with
    # a thin residue of carriers still crossing Baghdad and Damascus.
    residue = {"OIIX": {}, "ORBB": {"QTR": 2, "FDB": 1}, "OLBB": {"MEA": 1},
               "OSTT": {"QTR": 1}, "LLLL": {},
               "OMAE": {"UAE": 26, "FDB": 18, "ETD": 11, "QTR": 9},
               "OTDF": {"QTR": 22, "UAE": 6}, "OBBB": {"GFA": 9, "QTR": 5}}
    for offset in range(7):
        d = (today - timedelta(days=offset)).isoformat()
        for fir, carriers_seen in residue.items():
            for carrier, n in carriers_seen.items():
                jitter = max(0, n + random.randint(-1, 1))
                if jitter:
                    conn.execute(
                        "INSERT OR REPLACE INTO fir_transit VALUES (?,?,?,?)",
                        (d, fir, carrier, jitter))

    # Replay stop/resume detection day by day so the demo carries real event
    # history rather than a single snapshot.
    for offset in range(29, -1, -1):
        suspensions.detect(conn, today - timedelta(days=offset))

    conn.execute(
        "INSERT OR REPLACE INTO run_log VALUES (?,?,?,?)",
        (datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
         "demo_seed", 1, f"legs={len(rows)} DEMO DATA"),
    )
    conn.commit()
    print(f"seeded {len(rows)} synthetic legs across {days} days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--wipe", action="store_true")
    a = ap.parse_args()
    seed(a.days, a.wipe)
