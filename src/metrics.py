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

    A leg whose departure and arrival are the same airport is dropped. It is
    not a city pair, it is OpenSky's arrival estimate failing and falling back
    to where the aircraft started: QTR401 and QTR847 are real Doha departures
    to real places, filed here as OTHH-OTHH. Left in, they build baselines for
    routes that do not exist -- Doha-Doha at 71.6 departures a week, Dubai-
    Dubai at 47.4, 189 a week across 14 such rows, 3.6% of the whole baseline
    -- and those phantom routes then sit in the ratio denominator collecting
    almost no sightings, because nothing flies them.

    Dropped from the ROUTE view only. score_coverage counts the same legs
    straight off `flight`, which is right: a leg we could not resolve is still
    proof the receivers were working.
    """
    where = ("WHERE is_freight = 0 AND dep_icao IS NOT NULL "
             "AND arr_icao IS NOT NULL AND dep_icao <> arr_icao")
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

def _score_coverage_by_controls(conn: sqlite3.Connection, day: date) -> dict:
    """The old control-carrier count, kept only as a warm-up fallback.

    Its flaws are described in `score_coverage`; it is here because on the day
    the signal-volume table is created it holds nothing, and a coverage score
    that reads `outage` for a month while the new signal fills would freeze
    stop detection for that month.
    """
    controls = list(config.control_carriers().keys())
    if not controls:
        return {"day": day.isoformat(), "score": 1.0, "verdict": "ok",
                "control_flights": 0, "median_28d": 0.0}
    marks = ",".join("?" for _ in controls)
    today_n = conn.execute(
        f"SELECT COUNT(*) AS n FROM flight WHERE dep_date = ? AND carrier IN ({marks})",
        [day.isoformat(), *controls]).fetchone()["n"]
    history = conn.execute(
        f"""SELECT dep_date, COUNT(*) AS n FROM flight
            WHERE dep_date < ? AND dep_date >= ? AND carrier IN ({marks})
            GROUP BY dep_date""",
        [day.isoformat(), (day - timedelta(days=28)).isoformat(), *controls]).fetchall()
    counts = [r["n"] for r in history if r["n"] > 0]
    return _write_coverage(conn, day, today_n, counts)


def _write_coverage(conn: sqlite3.Connection, day: date, today_n: int,
                    counts: list) -> dict:
    """Shared scoring and persistence for both signals."""
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
        (day.isoformat(), today_n, median, round(score, 3), verdict))
    conn.commit()
    return {"day": day.isoformat(), "control_flights": today_n,
            "median_28d": round(median, 1), "score": round(score, 3),
            "verdict": verdict}


def record_fetch(conn: sqlite3.Connection, airport: str, direction: str,
                 raw: list) -> None:
    """Bucket one fetch's raw records by UTC day and store the counts.

    A fetch covers 48 hours and therefore spans two or three UTC days, so the
    records are bucketed by their own `firstSeen` rather than attributed to the
    day the fetch happened. Later runs re-read the same window and may see more
    of a day as OpenSky settles it, so the stored value is a high-water mark --
    the same reason `board_probe` keeps a MAX.
    """
    per_day: dict[str, int] = {}
    for rec in raw:
        first = rec.get("firstSeen")
        if not first:
            continue
        d = datetime.fromtimestamp(first, tz=timezone.utc).strftime("%Y-%m-%d")
        per_day[d] = per_day.get(d, 0) + 1
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    for d, n in per_day.items():
        conn.execute(
            """INSERT INTO fetch_probe (airport, direction, day, records, fetched_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(airport, direction, day) DO UPDATE SET
                 records = MAX(fetch_probe.records, excluded.records),
                 fetched_at = excluded.fetched_at""",
            (airport, direction, d, n, now))
    conn.commit()


def signal_volume(conn: sqlite3.Connection, day: date) -> int | None:
    """Records the receivers delivered on `day`, or None if we never asked."""
    row = conn.execute(
        "SELECT SUM(records) n, COUNT(*) c FROM fetch_probe WHERE day = ?",
        (day.isoformat(),)).fetchone()
    return row["n"] if row and row["c"] else None


def score_coverage(conn: sqlite3.Connection, day: date) -> dict:
    """How much of the network are we actually seeing today?

    Measures the pipe, not anybody's airline. Raw records delivered per day,
    before any carrier filter, against the median of the last 28 such days.

    This replaced a control group of six named carriers whose disappearance was
    supposed to mean the receivers had failed. Two things were wrong with it.
    The group was not six: measured 2026-09-02 over August, Emirates and Qatar
    supplied 11945 of 12168 control legs while Lufthansa supplied 1, Air France
    4 and KLM none -- so network health rested on two airlines at two airports,
    and Doha's fetch running at 0.27 of baseline on 2026-08-20 collapsed the
    score for the whole region. And three of the six have since been shown not
    to be flying at all, which is the one thing a control carrier may never be.

    Falls back to the old control-carrier count until `fetch_probe` has enough
    history to have a median, because a new signal with no reference period
    cannot tell a quiet day from a broken one.
    """
    today_n = signal_volume(conn, day)
    history = conn.execute(
        """SELECT day, SUM(records) AS n FROM fetch_probe
           WHERE day < ? AND day >= ? GROUP BY day""",
        [day.isoformat(), (day - timedelta(days=28)).isoformat()],
    ).fetchall()
    counts = [r["n"] for r in history if r["n"] > 0]

    if today_n is None or len(counts) < config.MIN_SIGNAL_HISTORY_DAYS:
        return _score_coverage_by_controls(conn, day)
    return _write_coverage(conn, day, today_n, counts)


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


def rolling_daily(conn: sqlite3.Connection, day: date) -> dict[tuple, dict[str, int]]:
    """The same trailing week as rolling_weekly, kept split by day.

    route_report needs to add up a route's departures over the days that
    route's own fetch delivered, and those days differ from route to route.
    A single pre-summed total cannot be filtered after the fact.
    """
    start = (day - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    out: dict[tuple, dict[str, int]] = {}
    for r in conn.execute(
            """SELECT carrier, dep_icao, arr_icao, day, SUM(departures) AS n
               FROM daily_route WHERE day BETWEEN ? AND ?
               GROUP BY carrier, dep_icao, arr_icao, day""",
            (start, day.isoformat())):
        out.setdefault((r["carrier"], r["dep_icao"], r["arr_icao"]), {})[r["day"]] = r["n"]
    return out


def load_baseline(conn: sqlite3.Connection) -> dict[tuple, float]:
    rows = conn.execute(
        "SELECT carrier, dep_icao, arr_icao, weekly_freq FROM baseline"
    ).fetchall()
    return {(r["carrier"], r["dep_icao"], r["arr_icao"]): r["weekly_freq"] for r in rows}


def baseline_airport_days(conn: sqlite3.Connection
                          ) -> tuple[dict[str, dict[str, int]], int]:
    """Days of the baseline window each airport was seen on, per direction.

    The reference period has the same problem as the present: it was harvested
    through whatever coverage existed at the time, and nothing recorded which
    of its days were observed. Counted across the frozen 92-day window as
    either endpoint, the monitored airports split cleanly: Doha, Dubai,
    Bahrain and Sharjah on 88 days, Abu Dhabi on 87, then Kuwait on 37, Amman
    on 24, Beirut on 17, and the seven no receiver covers on 0.

    A baseline built on the bottom half is not a low baseline, it is an
    unmeasured one, and dividing today's better-covered traffic by it reads as
    growth. Middle East Airlines flew 89 legs through Beirut in three months of
    the reference window and 174 in two observed days of August; published as a
    ratio that is 4000% of normal, which lands in NORMAL and hides the defect
    rather than showing it.

    route-level `sample_days` cannot answer this: a route flying twice a week
    honestly has 26 active days in 92. Observability is a property of the
    airport and of the direction fetched, which is why both are returned --
    see baseline_trusted() for why collapsing them is wrong.
    """
    win = conn.execute(
        "SELECT window_start, window_end FROM baseline LIMIT 1").fetchone()
    if not win or not win["window_start"]:
        return {"dep": {}, "arr": {}}, 0
    start, end = win["window_start"], win["window_end"]
    try:
        span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1
    except (TypeError, ValueError):
        # freeze() only ever writes ISO dates, so this means a hand-edited or
        # half-migrated row. Fall back to trusting the baseline rather than
        # blanking the whole site off a malformed string, but say so.
        LOG.warning("baseline window is not a date range (%r..%r); "
                    "cannot judge its coverage", start, end)
        return {"dep": {}, "arr": {}}, 0
    days: dict[str, dict[str, int]] = {"dep": {}, "arr": {}}
    for side, col in (("dep", "dep_icao"), ("arr", "arr_icao")):
        days[side] = {r["a"]: r["d"] for r in conn.execute(
            f"""SELECT {col} AS a, COUNT(DISTINCT day) AS d FROM daily_route
                WHERE day BETWEEN ? AND ? GROUP BY {col}""", (start, end))}
    return days, span


def baseline_control_drift(conn: sqlite3.Connection, cov: dict[str, str],
                           day: date) -> dict[str, float]:
    """Per airport: control traffic seen now, over control traffic in the baseline.

    Day counts alone cannot finish the job. Abu Dhabi's arrival fetch ran on 85
    of 92 baseline days and still returned 6.6 arrivals a day at a hub now
    showing 52 -- the fetch ran, the receivers did not deliver. Volume is what
    exposes that, but comparing an airport's own volume then against now is
    circular: it cannot tell coverage that improved from traffic that grew.

    The control carriers break the circle. They are already chosen for exactly
    this property -- operators whose disappearance means the sensors failed
    rather than the war escalated -- so a large jump in *their* rate at one
    airport is a statement about our receivers, not about aviation. Measured
    2026-08-12: Dubai 0.7x, Doha 0.8x, Sharjah 0.7x, against Abu Dhabi 18x,
    Amman 49x and Beirut 81x. Emirates and Qatar did not grow eighty-fold.

    One-sided on purpose. A ratio below 1 means today is thinner than the
    baseline, which is a finding about today and is what the coverage gate and
    the ratio itself are for. Only an inflated present impeaches the past.
    """
    controls = list(config.control_carriers())
    if not controls:
        return {}
    marks = ",".join("?" for _ in controls)
    win = conn.execute(
        "SELECT window_start, window_end FROM baseline LIMIT 1").fetchone()
    if not win or not win["window_start"]:
        return {}
    try:
        span = (datetime.fromisoformat(win["window_end"])
                - datetime.fromisoformat(win["window_start"])).days + 1
    except (TypeError, ValueError):
        return {}

    seen = observed_days(cov, day, WINDOW_DAYS)
    if not seen:
        return {}
    marks_days = ",".join("?" for _ in seen)

    def rates(sql, params, divisor):
        out: dict[str, float] = {}
        for r in conn.execute(sql, params):
            out[r["a"]] = out.get(r["a"], 0.0) + (r["n"] or 0) / divisor
        return out

    then = now = {}
    then = rates(
        f"""SELECT dep_icao AS a, SUM(departures) n FROM daily_route
            WHERE day BETWEEN ? AND ? AND carrier IN ({marks}) GROUP BY a
            UNION ALL
            SELECT arr_icao AS a, SUM(departures) n FROM daily_route
            WHERE day BETWEEN ? AND ? AND carrier IN ({marks}) GROUP BY a""",
        [win["window_start"], win["window_end"], *controls,
         win["window_start"], win["window_end"], *controls], span)
    now = rates(
        f"""SELECT dep_icao AS a, SUM(departures) n FROM daily_route
            WHERE day IN ({marks_days}) AND carrier IN ({marks}) GROUP BY a
            UNION ALL
            SELECT arr_icao AS a, SUM(departures) n FROM daily_route
            WHERE day IN ({marks_days}) AND carrier IN ({marks}) GROUP BY a""",
        [*seen, *controls, *seen, *controls], len(seen))

    drift: dict[str, float] = {}
    for a in set(then) | set(now):
        b = then.get(a, 0.0)
        # No control traffic at all in the baseline: nothing to anchor to, so
        # it cannot be vouched for.
        drift[a] = (now.get(a, 0.0) / b) if b > 0 else float("inf")
    return drift


def airport_side_coverage(conn: sqlite3.Connection, day: date,
                         lookback: int) -> dict[tuple[str, str], set[str]]:
    """Which days each monitored airport's departure and arrival fetch delivered.

    `was_observed` answers "did we look" for the network as a whole. It cannot
    answer "could we have seen THIS leg", and those are not the same question:
    a leg out of Doha is only ever visible through Doha's departure fetch, so
    when that one fetch comes back thin the leg is invisible on a day the
    network-wide verdict calls `ok`. See MIN_CURRENT_AIRPORT_COVERAGE for the
    measurement that made this concrete, and suspensions.silence for the use.

    Scored on control-carrier traffic, for the reason baseline_control_drift
    already gives: their volume at an airport is a statement about our
    receivers rather than about aviation. Restricted to monitored airports,
    because the far end of a route is never fetched -- Heathrow appears in the
    data only as whatever Dubai's fetch happened to carry, so its count says
    nothing about a fetch that was never made.

    An airport with no control traffic in the baseline gets no entry and can
    therefore never vouch for a silent day. That is the same refusal
    baseline_trusted makes: nothing to anchor to means nothing to conclude.

    Nor does a *near*-empty baseline, and that one bites harder because it
    looks like success. Abu Dhabi, Beirut and Amman were barely watched in the
    reference window, so their baseline rate is a sliver and today's traffic
    divides out at 16x, 38x and 46x -- clearing any floor trivially while
    saying nothing about how much we see. Left unguarded on 2026-08-20 that
    admitted exactly the airports it was built to exclude: Bahrain-Abu Dhabi
    at 62 timetabled departures a week passed as measurable on five days that
    produced zero sightings, and dragged Etihad to 10% of its own schedule.
    The ceiling is MAX_BASELINE_CONTROL_DRIFT, the same constant and the same
    reasoning baseline_trusted uses -- an inflated present impeaches the past
    it is measured against.
    """
    controls = list(config.control_carriers())
    monitored = set(config.airports())
    if not controls or not monitored:
        return {}
    marks = ",".join("?" for _ in controls)
    keep = ",".join("?" for _ in monitored)
    win = conn.execute(
        "SELECT window_start, window_end FROM baseline LIMIT 1").fetchone()
    if not win or not win["window_start"]:
        return {}
    try:
        span = (datetime.fromisoformat(win["window_end"])
                - datetime.fromisoformat(win["window_start"])).days + 1
    except (TypeError, ValueError):
        return {}

    days = [(day - timedelta(days=o)).isoformat() for o in range(lookback)]
    marks_days = ",".join("?" for _ in days)
    sides = (("dep", "dep_icao"), ("arr", "arr_icao"))

    baseline: dict[tuple[str, str], float] = {}
    for side, col in sides:
        for r in conn.execute(
                f"""SELECT {col} AS a, SUM(departures) n FROM daily_route
                    WHERE day BETWEEN ? AND ? AND carrier IN ({marks})
                      AND {col} IN ({keep}) GROUP BY a""",
                [win["window_start"], win["window_end"], *controls, *monitored]):
            baseline[(r["a"], side)] = (r["n"] or 0) / span

    ok: dict[tuple[str, str], set[str]] = {}
    for side, col in sides:
        for r in conn.execute(
                f"""SELECT {col} AS a, day, SUM(departures) n FROM daily_route
                    WHERE day IN ({marks_days}) AND carrier IN ({marks})
                      AND {col} IN ({keep}) GROUP BY a, day""",
                [*days, *controls, *monitored]):
            base = baseline.get((r["a"], side), 0.0)
            if base <= 0:
                continue
            if (r["n"] or 0) / base >= config.MIN_CURRENT_AIRPORT_COVERAGE:
                ok.setdefault((r["a"], side), set()).add(r["day"])

    # Drop whole airport-directions whose anchor cannot bear weight. Done
    # after the per-day pass rather than inside it, because the test is about
    # the window as a whole: one busy day does not rehabilitate a baseline
    # that was never collected.
    drift = baseline_control_drift(conn, coverage_map(conn), day)
    return {k: v for k, v in ok.items()
            if drift.get(k[0], float("inf")) <= config.MAX_BASELINE_CONTROL_DRIFT}


def nameable_days(conn: sqlite3.Connection, day: date,
                  lookback: int) -> dict[str, set[str]]:
    """Days each airport could be identified at all, monitored or not.

    Every leg has two ends and OpenSky resolves them independently: one is
    known from the fetch that returned the leg, the other is estimated from
    the track, and that estimate needs receivers there. Where it fails the leg
    lands with no usable endpoint and simply is not the route any more.

    Which is indistinguishable, from inside our data, from the route stopping.
    Measured 2026-08-21 over the trailing week: Delhi, Mumbai, Bangalore,
    Heathrow, Paris, Atlanta and Budapest all resolved on 5 or 6 days of 7,
    while Kolkata and Al Ain resolved on **none** -- and Kolkata had been
    steady at 2-6 departures a day through the whole reference window. Two
    stops opened against Kolkata that morning, Qatar's and Emirates', which is
    both carriers' Kolkata service disappearing on the same day. Airlines do
    not coordinate like that; receivers do.

    Monitored airports need this too, and that gap published a false finding.
    Kuwait carried KAC 435, Qatar 322, Emirates 290, Gulf Air 187, Air Arabia
    145 and flydubai 71 departures through the reference window and **zero
    from anyone** across 2026-08-15..22. Six carriers do not stop together;
    Kuwait went dark to us. Because OKBK is on the monitored list it was
    exempt from this test, and Dubai's healthy arrival fetch was allowed to
    vouch for Kuwait-Dubai legs on the reasoning that it would have caught
    them -- which is true only if the far end can still be named as Kuwait.
    Kuwait Airways was published at 0% of baseline, SUSPENDED, off that.

    Heathrow over the same days looks like the opposite case: Qatar 59,
    Emirates 46, Etihad 24, Royal Jordanian 19, MEA 18, Gulf Air 13, and
    British Airways zero. Same fetches, six carriers present, one absent -- so
    this test lets it through, and on 2026-08-21 that was read as a finding.
    It is not. The absence is per operator and this test cannot see operators;
    `carrier_visibility` below is the one that can.

    Counted across every carrier on purpose. Asking whether THIS route was
    seen would be circular -- that is the very thing in question. Asking
    whether the airport exists in our data at all is not.
    """
    days = [(day - timedelta(days=o)).isoformat() for o in range(lookback)]
    marks = ",".join("?" for _ in days)
    out: dict[str, set[str]] = {}
    for col in ("dep_icao", "arr_icao"):
        for r in conn.execute(
                f"""SELECT {col} AS a, day FROM daily_route
                    WHERE day IN ({marks}) AND departures > 0
                    GROUP BY a, day""", days):
            out.setdefault(r["a"], set()).add(r["day"])
    return out


def carrier_visibility(conn: sqlite3.Connection, day: date,
                       lookback: int) -> dict[str, float]:
    """How much of each carrier's own baseline our feed still delivers.

    The four gates before this one are geographic -- did this airport's fetch
    run, did that one deliver its usual volume, can either end be named, did
    the return leg fly. None of them can separate a British Airways from an
    Emirates, because on 2026-08-19 both flew Heathrow to Dubai, on the same
    day, through the same receivers, and only one of them is in our data.
    Asked by origin airport rather than by callsign, so that no parsing choice
    could hide the answer, seventy-one arrivals into Dubai came from European
    airports and every single one was Emirates or flydubai.

    So ask about the operator directly: legs seen over the days we observed,
    against what this carrier's baseline predicts for those same days. It is
    the airport diagnostic -- list every carrier there, then and now -- turned
    ninety degrees, and it is deliberately blunt. A carrier either shows up in
    our feed at roughly the rate it should, or it does not show up at all;
    measured over the thirteen observed days to 2026-08-23 there is nothing
    between 0.08 and 0.68.

    Carriers with no baseline get no entry. There is nothing to measure them
    against, and their routes are not comparable anyway.
    """
    days = observed_days(coverage_map(conn), day, lookback)
    if not days:
        return {}
    marks = ",".join("?" for _ in days)
    seen = {r["carrier"]: r["n"] for r in conn.execute(
        f"""SELECT carrier, SUM(departures) AS n FROM daily_route
            WHERE day IN ({marks}) GROUP BY carrier""", days)}
    out: dict[str, float] = {}
    for r in conn.execute(
            "SELECT carrier, SUM(weekly_freq) AS w FROM baseline GROUP BY carrier"):
        expected = r["w"] * len(days) / 7
        if expected > 0:
            out[r["carrier"]] = seen.get(r["carrier"], 0) / expected
    return out


def baseline_trusted(dep: str, arr: str, bl_days: dict[str, dict[str, int]],
                     floor: float, monitored: set[str],
                     drift: dict[str, float]) -> bool:
    """Could this route have entered the baseline at all?

    The harvest fetched departures and arrivals separately at each monitored
    airport, so direction is not decoration. A leg out of Abu Dhabi to a
    non-monitored airport could only ever arrive through the Abu Dhabi
    *departure* fetch, which ran on 43 of 92 days -- while Abu Dhabi as an
    arrival was seen on 87, because the other end was Dubai or Doha. Taking the
    better of the two numbers trusts a baseline that was never collected.
    """
    def ok(airport: str, side: str) -> bool:
        if airport not in monitored:
            return False
        if bl_days[side].get(airport, 0) < floor:
            return False
        return drift.get(airport, float("inf")) <= config.MAX_BASELINE_CONTROL_DRIFT

    return ok(dep, "dep") or ok(arr, "arr")


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

    # And the same question again, one level finer. `seen` is a property of the
    # network; whether we could have seen THIS route is a property of one
    # airport and one fetch direction, and the two disagree. Across
    # 2026-08-15..19, every day of it `ok`, Doha's departure fetch ran at
    # 0.27-0.56 of baseline while its arrival fetch held at 0.66 -- so a Doha
    # departure divided five days of half-sight by a full week of baseline and
    # published the shortfall as an airline decision. Qatar Airways read 47%
    # that way on 2026-08-20.
    seen_set = set(seen)
    ap_cov = airport_side_coverage(conn, day, WINDOW_DAYS)
    nameable = nameable_days(conn, day, WINDOW_DAYS)
    per_day = rolling_daily(conn, day)

    # And once more, of the operator. Every gate above is geographic and none
    # of them can tell a carrier that stopped from one this feed does not
    # carry -- BA and Emirates fly the same pair on the same day and only one
    # is in our data. Asked over a much wider span than the ratio window,
    # because it is a question about the feed rather than about the week.
    car_vis = carrier_visibility(conn, day, config.CARRIER_VISIBILITY_DAYS)
    bl_blind = config.baseline_blind_carriers()

    # The same question asked of the reference period. An airport we barely saw
    # back then cannot anchor a percentage now.
    bl_days, bl_span = baseline_airport_days(conn)
    bl_floor = bl_span * config.MIN_BASELINE_COVERAGE
    monitored = set(config.airports())
    bl_drift = baseline_control_drift(conn, cov, day)

    keys = set(base) | set(current)
    routes = []
    for key in sorted(keys):
        carrier, dep, arr = key
        if carrier not in tracked:
            continue
        raw = float(current.get(key, 0))
        bl = float(base.get(key, 0.0))
        bl_trusted = baseline_trusted(dep, arr, bl_days, bl_floor,
                                      monitored, bl_drift)
        # Coverage says the baseline was collected; this says it is big enough
        # to divide by. Under one departure a week, a seven-day window expects
        # less than one flight and the ratio is noise either way.
        # Which of the observed days this particular route could have been
        # seen on. A leg is caught by the departure fetch at its origin or the
        # arrival fetch at its destination, and by nothing else.
        vis = seen_set & (ap_cov.get((dep, "dep"), set())
                          | ap_cov.get((arr, "arr"), set()))
        # Both endpoints have to be identifiable before a leg can be counted
        # against this route; see nameable_days.
        for ap in (dep, arr):
            vis &= nameable.get(ap, set())
        # Numerator and denominator have to describe the same days. Summing a
        # whole calendar week and dividing by a week we only half-saw is the
        # 2026-08-12 mistake at a finer grain.
        raw_vis = sum(n for d, n in per_day.get(key, {}).items() if d in vis)
        vis_scale = WINDOW_DAYS / len(vis) if vis else None
        fetch_enough = len(vis) >= config.MIN_OBSERVED_DAYS

        comparable = (bl_trusted and bl >= config.MIN_BASELINE_WEEKLY
                      and carrier not in bl_blind)
        # A carrier with no baseline entry cannot be judged either way, and its
        # routes are not comparable regardless; let it through here.
        carrier_seen = (car_vis.get(carrier, 1.0)
                        >= config.MIN_CARRIER_VISIBILITY)
        # Kept apart from `comparable` on purpose: they fail for opposite
        # reasons and the reader is owed the difference. `comparable` false
        # means the REFERENCE period cannot be divided by, which renders as
        # NO BASELINE. `scored` false with `comparable` true means the
        # reference is fine and the PRESENT was not seen -- that is UNKNOWN,
        # and calling it NO BASELINE would blame the wrong end.
        # `carrier_seen` belongs on this side of the line, not with
        # `comparable`: the reference period is fine and it is the present we
        # cannot see, which is exactly what UNKNOWN means here.
        scored = comparable and fetch_enough and carrier_seen

        if not comparable:
            # Treated exactly like having no baseline, because that is what an
            # unobserved reference period is. classify() renders it NEW when
            # the carrier is flying, which the dashboard labels NO BASELINE.
            status, ratio = classify(raw, 0.0)
        elif enough and scored:
            # Scale what we saw up to a full week before comparing -- over this
            # route's own visible days, not the network's.
            status, ratio = classify(raw_vis * vis_scale, bl)
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
            "weekly_scaled": (round(raw_vis * vis_scale, 1)
                              if (enough and scored) else None),
            "baseline_weekly": round(bl, 1),
            "baseline_days": max(bl_days["dep"].get(dep, 0),
                                 bl_days["arr"].get(arr, 0)) if bl_span else 0,
            "baseline_trusted": bl_trusted,
            "comparable": comparable,
            "scored": scored,
            "ratio": ratio,
            "status": status,
            "silent_days": quiet,
            # This route's own basis, not the network's. They differ, and the
            # difference is the whole point of the field.
            "observed_days": len(vis),
        })

    return {
        "day": day.isoformat(),
        "coverage": coverage,
        "observed_days": len(seen),
        "window_days": WINDOW_DAYS,
        "min_observed_days": config.MIN_OBSERVED_DAYS,
        "ratios_published": enough,
        "baseline_window_days": bl_span,
        "baseline_min_days": round(bl_floor, 1),
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
            "weekly_scaled": r["weekly_scaled"],
            "baseline_weekly": r["baseline_weekly"],
            "silent_days": r["silent_days"],
            "observed_days": r["observed_days"],
        })
    order = {"critical": 0, "high": 1, "medium": 2}
    return sorted(out, key=lambda a: (order[a["severity"]], -a["baseline_weekly"]))
