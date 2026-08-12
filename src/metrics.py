"""Everything that turns raw legs into a defensible operational picture.

Design rule: no status is ever published without the coverage verdict that
produced it. A missing flight and a missing receiver look identical in the raw
data, and conflating them is how a monitor loses its credibility.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

from . import config

LOG = logging.getLogger("gulfwatch.metrics")


def today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


def reference_day() -> date:
    """The most recent UTC day whose data has settled.

    Analysis anchors here, not on today. See config.SETTLE_LAG_DAYS.
    """
    return today_utc() - timedelta(days=config.SETTLE_LAG_DAYS)


# --- Rollups ---------------------------------------------------------------

def rebuild_daily(conn: sqlite3.Connection, since: str | None = None,
                  until: str | None = None) -> None:
    """Recompute daily_route from the flight table.

    Bounded at both ends so one window can be folded in on its own. The
    backfill needs that: it drops each harvested window's raw legs once they
    are rolled up, so a rebuild reaching wider than the legs still present
    would erase the days whose legs are already gone.
    """
    where = "WHERE is_freight = 0 AND dep_icao IS NOT NULL AND arr_icao IS NOT NULL"
    params: list = []
    bounds: list[str] = []
    dparams: list = []
    if since:
        where += " AND dep_date >= ?"
        params.append(since)
        bounds.append("day >= ?")
        dparams.append(since)
    if until:
        where += " AND dep_date <= ?"
        params.append(until)
        bounds.append("day <= ?")
        dparams.append(until)
    conn.execute(
        "DELETE FROM daily_route" + (" WHERE " + " AND ".join(bounds) if bounds else ""),
        dparams,
    )
    conn.execute(
        f"""
        INSERT INTO daily_route (day, carrier, dep_icao, arr_icao, departures)
        SELECT dep_date, carrier, dep_icao, arr_icao, COUNT(*)
        FROM flight
        {where}
        GROUP BY dep_date, carrier, dep_icao, arr_icao
        """,
        params,
    )
    conn.commit()


# --- Coverage health -------------------------------------------------------

def score_coverage(conn: sqlite3.Connection, day: date) -> dict:
    """How much of the network are we actually seeing today?

    Uses control-group carriers only -- operators whose disappearance would
    mean the sensors failed, not that the war escalated.
    """
    controls = list(config.control_carriers().keys())
    if not controls:
        return {"day": day.isoformat(), "score": 1.0, "verdict": "ok",
                "control_flights": 0, "median_28d": 0.0}

    marks = ",".join("?" for _ in controls)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM flight WHERE dep_date = ? AND carrier IN ({marks})",
        [day.isoformat(), *controls],
    ).fetchone()
    today_n = row["n"]

    history = conn.execute(
        f"""
        SELECT dep_date, COUNT(*) AS n FROM flight
        WHERE dep_date < ? AND dep_date >= ? AND carrier IN ({marks})
        GROUP BY dep_date
        """,
        [day.isoformat(), (day - timedelta(days=28)).isoformat(), *controls],
    ).fetchall()

    counts = [r["n"] for r in history if r["n"] > 0]
    median = statistics.median(counts) if counts else 0.0

    if median <= 0 and today_n > 0:
        score, verdict = 1.0, "ok"      # cold start, but traffic is flowing
    elif median <= 0:
        # No reference period AND nothing visible today: we are blind, not
        # healthy. Blind must never read as ok -- suspensions.detect() only
        # touches state on ok days, so this is the one line standing between a
        # broken OpenSky credential and a feed announcing that every carrier in
        # the Gulf has stopped flying.
        score, verdict = 0.0, "outage"
    else:
        score = min(today_n / median, 1.5)
        if score >= config.COVERAGE_OK:
            verdict = "ok"
        elif score >= config.COVERAGE_DEGRADED:
            verdict = "degraded"
        else:
            verdict = "outage"

    conn.execute(
        """INSERT INTO coverage (day, control_flights, median_28d, score, verdict)
           VALUES (?,?,?,?,?)
           ON CONFLICT(day) DO UPDATE SET
             control_flights=excluded.control_flights, median_28d=excluded.median_28d,
             score=excluded.score, verdict=excluded.verdict""",
        (day.isoformat(), today_n, median, round(score, 3), verdict),
    )
    conn.commit()
    return {"day": day.isoformat(), "control_flights": today_n,
            "median_28d": round(median, 1), "score": round(score, 3),
            "verdict": verdict}


# --- Frequency -------------------------------------------------------------

WINDOW_DAYS = 7


def rolling_weekly(conn: sqlite3.Connection, day: date) -> dict[tuple, int]:
    """Departures in the trailing 7 days per (carrier, dep, arr).

    Raw counts, deliberately: this reports what was seen, and route_report
    scales it by how many of the seven days were actually observed.

    Weekly, not daily, on purpose: a large share of these routes run 2-4x per
    week, so a daily count is mostly day-of-week noise.
    """
    start = (day - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    rows = conn.execute(
        """SELECT carrier, dep_icao, arr_icao, SUM(departures) AS n
           FROM daily_route WHERE day BETWEEN ? AND ?
           GROUP BY carrier, dep_icao, arr_icao""",
        (start, day.isoformat()),
    ).fetchall()
    return {(r["carrier"], r["dep_icao"], r["arr_icao"]): r["n"] for r in rows}


def load_baseline(conn: sqlite3.Connection) -> dict[tuple, float]:
    rows = conn.execute(
        "SELECT carrier, dep_icao, arr_icao, weekly_freq FROM baseline"
    ).fetchall()
    return {(r["carrier"], r["dep_icao"], r["arr_icao"]): r["weekly_freq"] for r in rows}


def classify(current: float, baseline: float) -> tuple[str, float | None]:
    if baseline <= 0:
        return ("NEW" if current > 0 else "UNKNOWN"), None
    ratio = current / baseline
    if ratio >= config.STATUS_NORMAL_MIN:
        status = "NORMAL"
    elif ratio >= config.STATUS_REDUCED_MIN:
        status = "REDUCED"
    elif ratio >= config.STATUS_MINIMAL_MIN:
        status = "MINIMAL"
    else:
        status = "SUSPENDED"
    return status, round(ratio, 3)


# A day with no coverage row is not a quiet day, it is a day nobody looked at.
# Three separate bugs came from letting a missing row default to "ok", so the
# state has a name and callers ask for it rather than inferring it.
NEVER_OBSERVED = "never_observed"


def coverage_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["day"]: r["verdict"]
            for r in conn.execute("SELECT day, verdict FROM coverage").fetchall()}


def verdict_for(cov: dict[str, str], day: str) -> str:
    """The coverage verdict for `day`, naming the absent case instead of hiding it."""
    return cov.get(day, NEVER_OBSERVED)


def was_observed(cov: dict[str, str], day: str) -> bool:
    """True only if we looked at `day` and what we saw passed the gate."""
    return cov.get(day) == "ok"


def observed_days(cov: dict[str, str], day: date, window: int) -> list[str]:
    """Which of the `window` days ending on `day` we actually observed."""
    return [d for d in ((day - timedelta(days=o)).isoformat() for o in range(window))
            if was_observed(cov, d)]


def rescore_recent(conn: sqlite3.Connection, day: date,
                   days: int | None = None) -> list[tuple[str, str, str]]:
    """Re-judge coverage for recent days, because their data can arrive later.

    score_coverage only ever wrote a verdict for the run's own reference day.
    Ingest reads a 48h window and the backfill writes months at a time, so a
    day routinely gains its flights after it was already judged -- and the
    judgement was never revisited. Measured 2026-08-12: 2026-08-10 held 1147
    flight rows and 554 control flights while its coverage row still read
    `control_flights=0, verdict=outage`, written when the allowance was spent.

    That was harmless while nothing consulted the table. It is not harmless now
    that silence is gated on it: a stale `outage` permanently excludes a day we
    can see perfectly well.
    """
    days = days or config.COVERAGE_RESCORE_DAYS
    changed = []
    for offset in range(days):
        d = day - timedelta(days=offset)
        row = conn.execute("SELECT verdict FROM coverage WHERE day = ?",
                           (d.isoformat(),)).fetchone()
        before = row["verdict"] if row else NEVER_OBSERVED
        after = score_coverage(conn, d)["verdict"]
        if after != before:
            changed.append((d.isoformat(), before, after))
    conn.commit()
    for d, before, after in changed:
        LOG.info("coverage rescored %s: %s -> %s", d, before, after)
    return changed


def silent_days(conn: sqlite3.Connection, key: tuple, day: date,
                limit: int = 14, cov: dict[str, str] | None = None) -> int:
    """Consecutive OBSERVED days with zero departures, walking back from `day`.

    Only days that passed the coverage gate are counted. A day we did not look
    at is skipped: it neither counts as silence nor resets it, which is the
    same rule suspensions.silence() applies and for the same reason.

    Without that rule this counted blind days as proven silence. Measured on
    2026-08-12: of the 14 days behind the reference day, 3 had passed the gate,
    7 were `outage` (the backfill had spent the OpenSky allowance, so every
    carrier logged zero) and 4 had no coverage row at all. With
    SUSPENSION_CONFIRM_DAYS at 3, a carrier missing from one observed day plus
    two blind ones was published as SUSPENDED -- the field the JSON API leads
    with. The whole project turns on not making that mistake.

    The walk still spans `limit` calendar days rather than `limit` observed
    ones. Reaching further back to make up the shortfall would answer a
    question about last fortnight with traffic from a different month.
    """
    carrier, dep, arr = key
    if cov is None:
        cov = coverage_map(conn)
    rows = conn.execute(
        """SELECT day, departures FROM daily_route
           WHERE carrier=? AND dep_icao=? AND arr_icao=? AND day <= ? AND day >= ?""",
        (carrier, dep, arr, day.isoformat(),
         (day - timedelta(days=limit)).isoformat()),
    ).fetchall()
    seen = {r["day"]: r["departures"] for r in rows}
    n = 0
    for offset in range(limit):
        d = (day - timedelta(days=offset)).isoformat()
        if cov.get(d) != "ok":
            continue
        if seen.get(d, 0) > 0:
            break
        n += 1
    return n


def route_report(conn: sqlite3.Connection, day: date | None = None) -> dict:
    """The main analytical output: every baselined route, scored."""
    day = day or reference_day()
    coverage = score_coverage(conn, day)
    current = rolling_weekly(conn, day)
    base = load_baseline(conn)
    carriers = config.carriers()
    tracked = set(config.tracked_carriers())
    cov = coverage_map(conn)          # read once, not once per route

    # The rolling window is seven calendar days; how many of them we actually
    # saw is a different number, and every ratio below depends on it. Comparing
    # traffic from two observed days against a seven-day baseline is not a
    # measurement of a cut, it is a measurement of our own downtime -- on
    # 2026-08-12 it published Qatar Airways, Air Arabia and flydubai at 20-30%
    # of normal while all three were flying about their usual schedule.
    seen = observed_days(cov, day, WINDOW_DAYS)
    enough = len(seen) >= config.MIN_OBSERVED_DAYS
    scale = WINDOW_DAYS / len(seen) if seen else None

    keys = set(base) | set(current)
    routes = []
    for key in sorted(keys):
        carrier, dep, arr = key
        if carrier not in tracked:
            continue
        raw = float(current.get(key, 0))
        bl = float(base.get(key, 0.0))

        if enough:
            # Scale what we saw up to a full week before comparing.
            status, ratio = classify(raw * scale, bl)
        else:
            # Withhold rather than publish a percentage the window cannot
            # carry. The raw count and observed_days still go out, so a reader
            # can see exactly what the silence rests on.
            status, ratio = "UNKNOWN", None

        quiet = silent_days(conn, key, day, cov=cov) if status == "SUSPENDED" else 0

        # A route is only *called* suspended after it has been quiet long
        # enough, and never while coverage is bad.
        confirmed = status == "SUSPENDED" and quiet >= config.SUSPENSION_CONFIRM_DAYS
        if status == "SUSPENDED" and not confirmed:
            status = "MINIMAL"
        if coverage["verdict"] == "outage":
            status = "UNKNOWN"

        routes.append({
            "carrier": carrier,
            "carrier_name": carriers.get(carrier, {}).get("name", carrier),
            "origin": dep,
            "destination": arr,
            "weekly_frequency": int(raw),
            "weekly_scaled": round(raw * scale, 1) if enough else None,
            "baseline_weekly": round(bl, 1),
            "ratio": ratio,
            "status": status,
            "silent_days": quiet,
            "observed_days": len(seen),
        })

    return {
        "day": day.isoformat(),
        "coverage": coverage,
        "observed_days": len(seen),
        "window_days": WINDOW_DAYS,
        "min_observed_days": config.MIN_OBSERVED_DAYS,
        "ratios_published": enough,
        "routes": routes,
    }


def alerts_from(report: dict) -> list[dict]:
    """Only material changes, and nothing at all when coverage is degraded."""
    verdict = report["coverage"]["verdict"]
    if verdict != "ok":
        return []
    out = []
    for r in report["routes"]:
        if r["status"] == "SUSPENDED":
            severity = "critical"
        elif r["status"] == "MINIMAL":
            severity = "high"
        elif r["status"] == "REDUCED":
            severity = "medium"
        else:
            continue
        out.append({
            "severity": severity,
            "carrier": r["carrier"],
            "carrier_name": r["carrier_name"],
            "route": f"{r['origin']}-{r['destination']}",
            "status": r["status"],
            "weekly_frequency": r["weekly_frequency"],
            "baseline_weekly": r["baseline_weekly"],
            "silent_days": r["silent_days"],
        })
    order = {"critical": 0, "high": 1, "medium": 2}
    return sorted(out, key=lambda a: (order[a["severity"]], -a["baseline_weekly"]))
