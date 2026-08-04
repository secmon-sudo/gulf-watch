"""Build the frozen pre-escalation baseline.

Run this ONCE, before anything else is meaningful. Without a reference period
the monitor can count flights but cannot tell you whether that count is
normal, which is the entire question.

    python -m src.backfill --start 2025-11-01 --end 2026-01-31

This walks the chosen window in 7-day chunks (OpenSky's hard cap on the airport
endpoints) for every configured airport, then freezes mean weekly departures
per route into the `baseline` table.

Budget: 13 airports x 2 directions x 13 weeks = ~340 requests. Spread over a
couple of hours with the built-in pacing. Authenticated accounts have ample
credits for this; anonymous ones do not, so authenticate first.

    python -m src.backfill --freeze-only   # recompute from already-ingested legs
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config, db
from .opensky import OpenSky
from .parse import normalise

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("gulfwatch.backfill")


def harvest(start: str, end: str) -> int:
    conn = db.connect()
    api = OpenSky()
    if not api.authenticated:
        LOG.error(
            "Backfill needs an authenticated OpenSky client. Create an API "
            "client at opensky-network.org and set OPENSKY_CLIENT_ID/SECRET."
        )
        return 0

    t0 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    t1 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    carriers = config.carriers()
    total = 0

    for icao in config.airports():
        cursor = t0
        while cursor < t1:
            stop = min(cursor + 7 * 24 * 3600, t1)
            for fetch in (api.departures, api.arrivals):
                raw = fetch(icao, cursor, stop)
                rows = [r for r in (normalise(x, carriers, "opensky") for x in raw) if r]
                total += db.upsert_flights(conn, rows)
                time.sleep(2.0)
            LOG.info("%s %s..%s -> running total %s", icao,
                     datetime.utcfromtimestamp(cursor).date(),
                     datetime.utcfromtimestamp(stop).date(), total)
            cursor = stop
    return total


def freeze(start: str, end: str) -> int:
    """Collapse the window into mean weekly departures per route."""
    conn = db.connect()
    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1
    weeks = max(days / 7.0, 1.0)

    rows = conn.execute(
        """SELECT carrier, dep_icao, arr_icao, COUNT(*) AS n,
                  COUNT(DISTINCT dep_date) AS active_days
           FROM flight
           WHERE is_freight = 0 AND dep_date BETWEEN ? AND ?
             AND dep_icao IS NOT NULL AND arr_icao IS NOT NULL
           GROUP BY carrier, dep_icao, arr_icao
           HAVING n >= 2""",
        (start, end),
    ).fetchall()

    conn.execute("DELETE FROM baseline")
    conn.executemany(
        """INSERT INTO baseline (carrier, dep_icao, arr_icao, weekly_freq,
                                 sample_days, window_start, window_end)
           VALUES (?,?,?,?,?,?,?)""",
        [(r["carrier"], r["dep_icao"], r["arr_icao"],
          round(r["n"] / weeks, 2), r["active_days"], start, end) for r in rows],
    )
    conn.commit()
    LOG.info("froze %s routes over %s days (%.1f weeks)", len(rows), days, weeks)
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=config.BASELINE_START)
    ap.add_argument("--end", default=config.BASELINE_END)
    ap.add_argument("--freeze-only", action="store_true")
    args = ap.parse_args(argv)

    if not args.freeze_only:
        harvest(args.start, args.end)
    freeze(args.start, args.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
