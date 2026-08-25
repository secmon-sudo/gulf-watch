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

    python -m src.backfill --freeze-only   # recompute from the daily rollups
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config, db, metrics, schedules
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

# Raw legs younger than this are never pruned: coverage scoring reads a 28-day
# median off them and ingest rebuilds its rollups over 45. The baseline window
# sits months back, so this only ever guards against being pointed at a window
# that overlaps live data.
RAW_RETENTION_DAYS = 60


# Seen live, blind across the reference window. See observable_airports.
BASELINE_BLIND = {"OOMS"}


def observable_airports() -> list[str]:
    """The airports OpenSky can actually see.

    Derived from schedules.BLIND rather than written out again, because it was
    written out again: the same eight ICAO codes sat in run-backfill.sh and in
    the workflow's input default, and the workflow's scheduled trigger passes
    no inputs at all -- so the list has to come from somewhere that a cron run
    reaches too. Harvesting the other seven costs about half the daily
    allowance and returns zero flights, measured over a real 48h ingest.

    Muscat is the eighth, and it is only knowable by having harvested it. The
    live 48h ingest sees it, so BLIND does not list it, but across the frozen
    2025-11-01..2026-01-31 window it returned **zero legs on all thirteen of
    its slices** in the 2026-08-11 harvest. Thirteen slices is an eighth of the
    run and, at the pace a re-harvest can afford beside the daily ingest, more
    than a day of waiting for nothing.
    """
    return [icao for icao, cfg in config.airports().items()
            if cfg["iata"] not in schedules.BLIND and icao not in BASELINE_BLIND]


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


def compact(start: str, end: str) -> dict:
    """Fold a harvested window into daily_route and drop its raw legs.

    The database is committed to a public git repository, and SQLite is a
    binary file git cannot delta-compress: every run stores a whole fresh
    copy. Measured 2026-08-07 at 32 of 104 slices, 127,338 of 130,120 legs sat
    in the baseline window, `flight` and its three indexes were 19.5MB of a
    20.1MB file, and the file had gone 1.6MB -> 20MB in three days. Left
    alone that reaches ~65MB at the full 104 slices -- past GitHub's 50MB
    warning, within sight of the 100MB hard limit, with several hundred MB of
    history behind it.

    What the analysis actually reads is departures per route per day, which is
    what daily_route holds; freeze() derives the baseline from it. So the
    harvest is kept in the shape everything uses and the raw legs go.

    Only ever call this on a COMPLETE harvest. Pruning between runs would
    break the deduplication the `flight` primary key provides: the airports
    are harvested individually but fly to each other, so a Sharjah-Dubai leg
    arrives twice, once from Dubai's arrivals and once from Sharjah's
    departures. While both copies are rows they collapse onto one
    (icao24, first_seen). Fold the first away and the second re-lands as a
    new row and is counted a second time.

    Recent days are never touched either. score_coverage() counts raw legs
    over a 28-day median and report.py reads them for the activity window, so
    the retention floor sits well clear of both.
    """
    cutoff = (datetime.now(tz=timezone.utc).date()
              - timedelta(days=RAW_RETENTION_DAYS)).isoformat()
    if end >= cutoff:
        LOG.warning("not compacting %s..%s: inside the %s-day raw retention "
                    "window (cutoff %s)", start, end, RAW_RETENTION_DAYS, cutoff)
        return {"rolled_up": 0, "pruned": 0}

    conn = db.connect()
    raw = conn.execute(
        "SELECT COUNT(*) n FROM flight WHERE dep_date BETWEEN ? AND ?",
        (start, end)).fetchone()["n"]
    if not raw:
        # Already folded in. rebuild_daily() clears the range before it
        # regenerates it, so running again with no legs left to read would
        # delete the rollup and leave freeze() nothing to freeze.
        LOG.info("nothing to compact for %s..%s: raw legs already folded in",
                 start, end)
        conn.close()
        return {"rolled_up": 0, "pruned": 0}

    metrics.rebuild_daily(conn, since=start, until=end)
    rolled = conn.execute(
        "SELECT COUNT(*) n FROM daily_route WHERE day BETWEEN ? AND ?",
        (start, end)).fetchone()["n"]
    pruned = conn.execute(
        "DELETE FROM flight WHERE dep_date BETWEEN ? AND ?", (start, end)).rowcount
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    LOG.info("compacted %s..%s: %s rollup rows kept, %s raw legs dropped",
             start, end, rolled, pruned)
    return {"rolled_up": rolled, "pruned": pruned}


def freeze(start: str, end: str) -> int:
    """Collapse the window into mean weekly departures per route.

    Reads the daily rollup rather than the raw legs. The two give the same
    answer -- daily_route is grouped from exactly this window's rows under
    exactly this filter -- but only the rollup survives compact(), which drops
    the raw legs once they are folded in.
    """
    conn = db.connect()
    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1
    weeks = max(days / 7.0, 1.0)

    rows = conn.execute(
        """SELECT carrier, dep_icao, arr_icao, SUM(departures) AS n,
                  COUNT(DISTINCT day) AS active_days
           FROM daily_route
           WHERE day BETWEEN ? AND ?
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
                    help="comma-separated ICAO list. Defaults to the airports "
                         "OpenSky can actually see, which roughly halves the "
                         "harvest; pass 'all' for every airport in config.")
    args = ap.parse_args(argv)

    if args.freeze_only:
        freeze(args.start, args.end)
        return 0

    if not args.airports:
        picked = observable_airports()
    elif args.airports.strip().lower() == "all":
        picked = None
    else:
        picked = [a.strip().upper() for a in args.airports.split(",") if a.strip()]
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

    # Order matters: fold the window into daily_route and drop the raw legs
    # only now that every slice has landed and the flight table has deduped
    # them, then freeze off the rollup.
    compact(args.start, args.end)
    freeze(args.start, args.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
