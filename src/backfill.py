"""Build the frozen pre-escalation baseline.

Run this ONCE, before anything else is meaningful. Without a reference period
the monitor can count flights but cannot tell you whether that count is
normal, which is the entire question.

    python -m src.backfill --start 2025-11-01 --end 2026-01-31

This walks the chosen window for every configured airport, then freezes mean
weekly departures per route into the `baseline` table. The 7-day loop below is
just outer bookkeeping: OpenSky's airport endpoints are partitioned by UTC day
and refuse any request touching more than two of them, so opensky.py subdivides
each chunk again into 2-day requests.

Budget: 13 airports x 2 directions x ~46 two-day slices over a 92-day window
= ~1200 requests, paced at 2s. Expect a couple of hours. Authenticated accounts
have the credits for this; there is no anonymous option any more.

    python -m src.backfill --freeze-only   # recompute from already-ingested legs
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config, db
from .opensky import OpenSky, RateLimited
from .parse import normalise

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("gulfwatch.backfill")

# "Stopped early, resume tomorrow" -- the expected outcome on every run but the
# last. It needs its own code because an unhandled exception also exits 1, and
# a caller has to be able to tell "the allowance ran out, keep what landed"
# from "this broke, do not trust the db".
EXIT_INCOMPLETE = 2


def _slices(t0: int, t1: int) -> list[tuple[int, int]]:
    out, cursor = [], t0
    while cursor < t1:
        stop = min(cursor + 7 * 24 * 3600, t1)
        out.append((cursor, stop))
        cursor = stop
    return out


def harvest(start: str, end: str, airports: list[str] | None = None) -> dict:
    """Fetch every slice not already recorded as done.

    Returns {"legs", "done", "remaining"}. `remaining` > 0 means OpenSky cut us
    off; run again tomorrow and it picks up where it stopped.

    `airports` narrows the harvest. Worth using: measured over a real 48h
    ingest, 7 of the 15 configured airports returned zero flights -- OpenSky
    simply has no receiver coverage over Kuwait, Saudi Arabia, Iraq or Iran.
    Harvesting them spends about half the daily allowance to learn nothing.
    """
    conn = db.connect()
    api = OpenSky()
    if not api.authenticated:
        LOG.error(
            "Backfill needs an authenticated OpenSky client. Create an API "
            "client at opensky-network.org and set OPENSKY_CLIENT_ID/SECRET."
        )
        return {"legs": 0, "done": 0, "remaining": -1}

    t0 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    t1 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    carriers = config.carriers()

    done = {(r["airport"], r["window_start"], r["window_end"]) for r in conn.execute(
        "SELECT airport, window_start, window_end FROM backfill_progress")}
    picked = airports or list(config.airports())
    unknown = [a for a in picked if a not in config.airports()]
    if unknown:
        LOG.error("not in config/airports.yml: %s", ", ".join(unknown))
        return {"legs": 0, "done": 0, "remaining": -1}

    todo = [(icao, a, b) for icao in picked for a, b in _slices(t0, t1)
            if (icao, str(a), str(b)) not in done]
    if not todo:
        LOG.info("nothing left to harvest for %s..%s", start, end)
        return {"legs": 0, "done": len(done), "remaining": 0}

    LOG.info("%s slices to fetch (%s already done)", len(todo), len(done))
    total = completed = 0

    for icao, a, b in todo:
        try:
            legs = 0
            for fetch in (api.departures, api.arrivals):
                raw = fetch(icao, a, b)
                rows = [r for r in (normalise(x, carriers, "opensky") for x in raw) if r]
                legs += db.upsert_flights(conn, rows)
                time.sleep(2.0)
        except RateLimited as exc:
            # Stop cleanly. The slice is deliberately NOT marked done, so the
            # next run refetches it whole rather than trusting a half of it.
            LOG.warning("%s -- stopping with %s slices left; rerun after the "
                        "window resets and it resumes here",
                        exc, len(todo) - completed)
            return {"legs": total, "done": len(done) + completed,
                    "remaining": len(todo) - completed}

        conn.execute(
            """INSERT OR REPLACE INTO backfill_progress
               (airport, window_start, window_end, legs, done_at)
               VALUES (?,?,?,?,?)""",
            (icao, str(a), str(b), legs,
             datetime.now(tz=timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        total += legs
        completed += 1
        LOG.info("%s %s..%s -> %s legs (%s/%s slices)", icao,
                 datetime.utcfromtimestamp(a).date(),
                 datetime.utcfromtimestamp(b).date(), legs, completed, len(todo))

    return {"legs": total, "done": len(done) + completed, "remaining": 0}


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
    ap.add_argument("--airports", default=None,
                    help="comma-separated ICAO list; default is every airport "
                         "in config. Narrowing to the ones OpenSky can "
                         "actually see roughly halves the harvest.")
    args = ap.parse_args(argv)

    if args.freeze_only:
        freeze(args.start, args.end)
        return 0

    picked = ([a.strip().upper() for a in args.airports.split(",") if a.strip()]
              if args.airports else None)
    result = harvest(args.start, args.end, picked)
    if result["remaining"] != 0:
        # Freezing here would write a baseline built from whatever fraction
        # arrived and present it as the frozen reference. Every route we never
        # reached would silently have no baseline at all, and the ones we did
        # reach would be divided by the full window's weeks, understating them
        # several-fold. A missing baseline is recoverable; a confidently wrong
        # one poisons every number the project publishes.
        LOG.error("harvest incomplete (%s slices left) -- NOT freezing the "
                  "baseline. Rerun this command to resume, then it freezes "
                  "automatically.", result["remaining"])
        return EXIT_INCOMPLETE

    freeze(args.start, args.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
