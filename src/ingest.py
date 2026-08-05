"""Main ETL. Run me from GitHub Actions.

    python -m src.ingest              # normal incremental run
    python -m src.ingest --hours 168  # wider catch-up window
    python -m src.ingest --all-airports

Re-reading the last 48 hours on every run is intentional. OpenSky's airport
endpoints publish with a lag and backfill late, so a single forward-only pass
silently loses flights.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from . import (advisories, config, corroborate, db, firwatch, flightboard,
               metrics, schedules, suspensions)
from .opensky import OpenSky, RateLimited
from .parse import normalise

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
LOG = logging.getLogger("gulfwatch.ingest")


def pick_airports(run_index: int, all_airports: bool) -> dict:
    airports = config.airports()
    if all_airports:
        return airports
    return {
        k: v for k, v in airports.items()
        if v.get("priority", 1) == 1 or run_index % 4 == 0
    }


def run(hours: int, all_airports: bool, skip_fir: bool = False) -> dict:
    started = datetime.now(tz=timezone.utc)
    conn = db.connect()
    api = OpenSky()
    if not api.authenticated:
        LOG.warning(
            "no OPENSKY_CLIENT_ID/SECRET -- OpenSky closed anonymous access to "
            "the flights endpoints, so no flight history will be collected. "
            "FIR sampling and advisories still run."
        )

    end = int(started.timestamp())
    begin = int((started - timedelta(hours=hours)).timestamp())
    carriers = config.carriers()
    airports = pick_airports(started.hour, all_airports)

    total = 0
    exhausted = False
    for icao in airports:
        if exhausted:
            break
        for direction, fetch in (("dep", api.departures), ("arr", api.arrivals)):
            try:
                raw = fetch(icao, begin, end)
            except RateLimited as exc:
                # The next run re-reads the same 48 hours and upserts, so what
                # we miss here is picked up rather than lost. Keep going: the
                # FIR sampler, the advisories and the publish step cost no
                # OpenSky credits.
                LOG.warning("%s -- skipping the rest of the OpenSky fetches", exc)
                exhausted = True
                break
            rows = [r for r in (normalise(x, carriers, "opensky") for x in raw) if r]
            total += db.upsert_flights(conn, rows)
            LOG.info("%s %s: %s raw -> %s matched", icao, direction, len(raw), len(rows))
            time.sleep(1.0)

    since = (started - timedelta(days=45)).strftime("%Y-%m-%d")
    metrics.rebuild_daily(conn, since=since)

    fir_result = {} if skip_fir else firwatch.sample(conn, only_czib=False)
    czib_changes = advisories.fetch(conn)

    # The timetable layer. It costs no OpenSky credits and it is the only
    # source that says anything at all about Kuwait, Riyadh, Jeddah, Erbil,
    # Abha, Baghdad and Tehran, where every ADS-B receiver returns zero.
    #
    # refresh() only asks about pairs it has not seen in a week, so running
    # this on every ingest is self-limiting: 210 pairs a week is ~900 of the
    # free tier's 1000 monthly requests, and repeated runs inside the week
    # cost nothing. It stops by itself when the quota is gone.
    sched = schedules.refresh(conn)

    # Boards for the seven airports no receiver covers. Written to their own
    # table, never to `flight`, so a listing can never be counted as a
    # sighting by the coverage score or by stop detection.
    board = flightboard.sample(conn)

    # Score the last settled day, not the half-finished one we are standing in.
    ref = metrics.reference_day()
    coverage = metrics.score_coverage(conn, ref)

    # Stop/resume detection runs after coverage is scored for today, because it
    # refuses to touch state on a bad-coverage day.
    events = suspensions.detect(conn, ref)
    corro = corroborate.enrich(conn) if events["opened"] else {"checked": 0}

    detail = (f"legs={total} coverage={coverage['verdict']}"
              f"({coverage['score']}) stopped={events['opened']} "
              f"resumed={events['resumed']} czib_changes={len(czib_changes)} "
              f"sched={sched['fetched']}"
              + ("(no key)" if sched["no_key"] else "")
              + f" board={board['written']}"
              + (f" FLAGGED[{','.join(board['flagged'])}]"
                 if board["flagged"] else ""))
    conn.execute(
        "INSERT OR REPLACE INTO run_log (started_at, kind, ok, detail) VALUES (?,?,?,?)",
        (started.isoformat(timespec="seconds"), "ingest", 1, detail),
    )
    conn.commit()
    LOG.info("done: %s", detail)
    return {"legs": total, "coverage": coverage, "fir": fir_result,
            "czib_changes": czib_changes, "events": events, "corroboration": corro}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=config.INGEST_LOOKBACK_HOURS)
    ap.add_argument("--all-airports", action="store_true")
    ap.add_argument("--skip-fir", action="store_true")
    args = ap.parse_args(argv)
    run(args.hours, args.all_airports, args.skip_fir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
