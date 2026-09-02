"""Did they actually stop, when, and are they back?

Frequency ratios answer "how much". They cannot answer "did this carrier stop
serving Dubai on the 14th and resume on the 29th", which is the question that
matters when you are watching a conflict. That needs events with a start, a
duration and an end, carried across runs.

Three scopes, because "stopped" means different things:

  route    QTR|OTHH|OMDB   one city pair dropped
  station  QTR|OMDB        the carrier left an airport entirely
  region   QTR             the carrier vanished from every monitored airport

A station suspension is the headline. A region suspension usually means either
a total network shutdown or -- far more likely -- that something is wrong with
your data, so it is held to a much longer threshold.

Six rules keep this honest:

1. Silent days are COVERAGE-GATED. A day when the sensor network was degraded
   neither extends a silence streak nor breaks it; it is skipped. Otherwise
   every ADS-B outage manufactures a fake suspension with a precise-looking
   start date.
2. A suspension is only ever OPENED against a real baseline. No baseline means
   UNKNOWN, never "stopped".
3. It is also only ever opened against a real SIGHTING, and the silent days
   have to fit inside a bounded span of calendar days. Something has to stop
   before it can be stopped, and a week of silence has to mean a week.
4. Silence is gated PER AIRPORT AND PER FETCH DIRECTION, not only per day.
   The day-level verdict scores the whole network, so one hub's thin fetch
   hides inside an `ok`. A leg is seen through exactly one fetch, and on
   2026-08-20 that gap called Doha-Atlanta stopped while Atlanta-Doha was
   being observed three times a week.
5. And before a route stop is written, the OPPOSITE DIRECTION is checked.
   Scheduled service does not run one way; if the return leg flew, the
   outbound is missing from our data rather than from the sky. This is the
   test that caught all twelve false stops of 2026-08-20/21/23 -- by hand,
   after publishing, each time.
6. And the CARRIER has to be one this feed carries at all. The five rules
   above are geographic and none of them can separate a British Airways from
   an Emirates: on 2026-08-19 both flew Heathrow to Dubai, and only one is in
   our data. Measured that day, seventy-one arrivals into Dubai came from
   European airports and every one was Emirates or flydubai.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from . import config, metrics

LOG = logging.getLogger("gulfwatch.suspensions")

# Coverage-good silent days before we will call it a stop.
THRESHOLD = {"route": 7, "station": 10, "region": 21}

# ...and the calendar span those days must fit inside, at twice the threshold.
#
# The gating rule above counts observed days only, which is right, but it
# quietly changed what the threshold means: seven observed silent days can be
# spread across any number of weeks if coverage is sparse. On 2026-08-17 that
# opened 147 route stops at once. The entire observation record was seven ok
# days scattered over sixteen calendar days -- 08-01/02/03, then 08-10/11, then
# 08-15/16 -- so every route the receivers cannot see hit the route threshold
# on the first run after the gap. Requiring the seven to land inside fourteen
# days restores the reading "a week of silence" instead of "a week's worth of
# glimpses, whenever we happened to look".
SPAN_DAYS = {"route": 14, "station": 20, "region": 42}

# Ignore scopes too small to be meaningful (weekly departures in the baseline).
MIN_BASELINE = {"route": 1.0, "station": 2.0, "region": 4.0}

MAX_LOOKBACK = 400


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# --- Scope construction ----------------------------------------------------

def build_scopes(conn) -> dict[str, dict[str, dict]]:
    """Every scope we could suspend, with its baseline and airports."""
    tracked = set(config.tracked_carriers())
    rows = conn.execute(
        "SELECT carrier, dep_icao, arr_icao, weekly_freq FROM baseline"
    ).fetchall()

    scopes: dict[str, dict[str, dict]] = {"route": {}, "station": {}, "region": {}}
    for r in rows:
        c, dep, arr, wf = r["carrier"], r["dep_icao"], r["arr_icao"], r["weekly_freq"]
        if c not in tracked:
            continue
        scopes["route"].setdefault(
            f"{c}|{dep}|{arr}",
            {"carrier": c, "detail": f"{dep}-{arr}", "baseline": 0.0,
             "airports": {dep, arr}},
        )["baseline"] += wf
        for ap in (dep, arr):
            scopes["station"].setdefault(
                f"{c}|{ap}",
                {"carrier": c, "detail": ap, "baseline": 0.0, "airports": {ap}},
            )["baseline"] += wf
        scopes["region"].setdefault(
            c,
            {"carrier": c, "detail": "all monitored airports", "baseline": 0.0,
             "airports": set()},
        )["baseline"] += wf
        scopes["region"][c]["airports"] |= {dep, arr}
    return scopes


def _daily_counts(conn, scope: str, key: str) -> dict[str, int]:
    """Departures per day for a scope."""
    if scope == "route":
        c, dep, arr = key.split("|")
        rows = conn.execute(
            """SELECT day, SUM(departures) n FROM daily_route
               WHERE carrier=? AND dep_icao=? AND arr_icao=? GROUP BY day""",
            (c, dep, arr)).fetchall()
    elif scope == "station":
        c, ap = key.split("|")
        rows = conn.execute(
            """SELECT day, SUM(departures) n FROM daily_route
               WHERE carrier=? AND (dep_icao=? OR arr_icao=?) GROUP BY day""",
            (c, ap, ap)).fetchall()
    else:
        rows = conn.execute(
            "SELECT day, SUM(departures) n FROM daily_route WHERE carrier=? GROUP BY day",
            (key,)).fetchall()
    return {r["day"]: r["n"] for r in rows}


def _coverage_map(conn) -> dict[str, str]:
    # One definition of "did we look", shared with metrics, so the two modules
    # that measure silence cannot drift apart on what an absent day means.
    return metrics.coverage_map(conn)


def silence(counts: dict[str, int], cov: dict[str, str], day: date,
            span: int | None = None, visible: set[str] | None = None) -> dict:
    """Walk backwards. Returns gated silent days, last operating day, skips.

    `span` bounds the calendar window silent days may be drawn from; the walk
    for the last operating day is not bounded, because "when did we last see
    it fly" has to be answerable from further back than "has it been quiet for
    a week". See SPAN_DAYS.

    Days with bad coverage are skipped entirely -- they do not count as silence
    and they do not reset it. This is the difference between "they stopped on
    the 14th" and "our receivers died on the 14th".

    `visible` narrows the same idea from the network to this scope: the days
    on which the fetch that could have seen these legs actually delivered. A
    day the network calls `ok` is still skipped here if the one airport-and-
    direction this scope depends on came back thin. See
    metrics.airport_side_coverage.

    A day with NO coverage row is skipped for the same reason, and this is not
    a detail. It used to default to "ok", which read a day nobody looked at as
    a day that was looked at and found empty. The baseline harvest ends
    2026-01-31 and observation began 2026-08-01, so every scope had a silent
    stretch of about 190 unlooked-at days behind it. On 2026-08-12, the first
    run whose coverage gate passed, that opened 270 suspensions at once --
    7 whole carriers "stopped" since January, at `confidence: observed`, off a
    hole in the calendar. Absent is not empty.
    """
    span = span or MAX_LOOKBACK
    silent = skipped = 0
    last_flight = None
    for offset in range(MAX_LOOKBACK):
        d = day - timedelta(days=offset)
        key = d.isoformat()
        if not metrics.was_observed(cov, key):
            skipped += 1
            continue
        if counts.get(key, 0) > 0:
            last_flight = key
            break
        if visible is not None and key not in visible:
            skipped += 1
            continue
        if offset < span:
            silent += 1
    return {"silent_days": silent, "last_flight_on": last_flight,
            "coverage_days_skipped": skipped}


def visible_days(scope: str, key: str, meta: dict,
                 ap_cov: dict[tuple[str, str], set[str]],
                 nameable: dict[str, set[str]] | None = None) -> set[str]:
    """The days this scope could have been seen on, if it were flying.

    Direction is not decoration. A Doha-Atlanta leg reaches us only through
    Doha's departure fetch; the Atlanta-Doha leg only through Doha's arrival
    fetch. On 2026-08-20 the first was called stopped while the second was
    being observed three times a week, because both were scored against one
    network-wide `ok`. Asking per direction is what separates them.

    The far end of a route is unmonitored and contributes nothing on its own,
    so a route with no monitored endpoint returns the empty set and can never
    accumulate a silent day -- which is correct: we were never in a position
    to look.

    Either end can, however, take days AWAY. An endpoint has to be
    identifiable before a leg can be attributed to it, and when it is not, the
    leg stops being this route in our data without anything having stopped
    flying. See metrics.nameable_days -- Kolkata went unresolvable on
    2026-08-04 and took Qatar's and Emirates' Kolkata service with it, and
    Kuwait did the same to six carriers at once.
    """
    if scope == "route":
        _, dep, arr = key.split("|")
        days = ap_cov.get((dep, "dep"), set()) | ap_cov.get((arr, "arr"), set())
        if nameable is not None:
            for ap in (dep, arr):
                days &= nameable.get(ap, set())
        return days
    if scope == "station":
        _, ap = key.split("|")
        return ap_cov.get((ap, "dep"), set()) | ap_cov.get((ap, "arr"), set())
    seen: set[str] = set()
    for ap in meta["airports"]:
        seen |= ap_cov.get((ap, "dep"), set()) | ap_cov.get((ap, "arr"), set())
    return seen


def board_flew(conn, scope: str, key: str, since: str) -> str | None:
    """Did the airport boards list this carrier's own aeroplane? Returns the day.

    The second falsification, and the one `reverse_flew` cannot make. A route
    stop where both directions are equally invisible passes that test and is
    still wrong whenever the invisibility is ours -- ADS-B resolves an
    aircraft, not an operator, so a carrier it cannot attribute goes silent in
    both directions at once.

    Measured 2026-09-02 against the four stops then live: Royal Air Maroc was
    published as having stopped serving Doha while the board listed its own
    metal there once each way, and Air Arabia was published as having stopped
    Sharjah-Istanbul in BOTH directions while the board listed one departure
    and one arrival on its own aircraft. Three of four, and only Oman Air's
    Delhi-Dubai survived -- at Dubai it flies Muscat and Salalah and nothing
    else, which is what the board says too.

    `operated_by IS NULL` is not optional here. Counting codeshares would have
    let Qatar's aeroplanes vouch for British Airways, which is the error this
    whole source was added to end.
    """
    parts = key.split("|")
    carrier = parts[0]
    sql = ["""SELECT MAX(bf.day) d FROM board_flight bf
              JOIN board_probe bp ON bp.airport = bf.airport AND bp.day = bf.day
              WHERE bf.carrier = ? AND bf.day >= ? AND bf.operated_by IS NULL
                AND bp.verdict = 'ok'"""]
    args: list = [carrier, since]

    if scope == "station":
        sql.append("AND bf.airport = ?")
        args.append(parts[1])
    elif scope == "route":
        # The carrier flying somewhere else does not refute a claim about one
        # route: Oman Air's own metal at Jeddah and Riyadh says nothing about
        # Delhi-Dubai, which it genuinely does not operate -- at Dubai it flies
        # Muscat and Salalah and nothing more, exactly as the board shows.
        #
        # The board names the far end by IATA and the ledger by ICAO, so a
        # route is only checkable when both ends are airports we monitor. When
        # the far end cannot be resolved the answer is None and the stop
        # stands: this test may withhold a claim, never manufacture one.
        iata = {code: cfg["iata"] for code, cfg in config.airports().items()}
        dep, arr = parts[1], parts[2]
        legs = []
        if dep in iata and arr in iata:
            legs = [(dep, "dep", iata[arr]), (arr, "arr", iata[dep])]
        if not legs:
            return None
        sql.append("AND (" + " OR ".join(
            "(bf.airport=? AND bf.direction=? AND bf.other_iata=?)"
            for _ in legs) + ")")
        for leg in legs:
            args.extend(leg)

    row = conn.execute(" ".join(sql), args).fetchone()
    return row["d"] if row and row["d"] else None


def reverse_flew(conn, scope: str, key: str, since: str) -> str | None:
    """Was the opposite direction seen flying since `since`? Returns the day.

    Scheduled passenger service does not run one way. An aircraft that lands
    in Boston has to leave it, so if we can see the inbound and never the
    outbound, the outbound is missing from our data rather than from the sky
    -- OpenSky resolves the two ends of a leg independently and either can
    fail on its own.

    This is the test that caught every false stop this project has published
    since the coverage gates went in: nine on 2026-08-20 (Doha-Atlanta
    "stopped" while Atlanta-Doha flew five times), two on 2026-08-21, one on
    2026-08-23. Each time it was run by hand afterwards. Run it before.

    Route scope only. A station or region suspension is about an airport or a
    carrier, and has no opposite direction to ask about.
    """
    if scope != "route":
        return None
    carrier, dep, arr = key.split("|")
    row = conn.execute(
        """SELECT MAX(day) d FROM daily_route
           WHERE carrier=? AND dep_icao=? AND arr_icao=? AND day >= ?
             AND departures > 0""",
        (carrier, arr, dep, since)).fetchone()
    return row["d"] if row and row["d"] else None


def first_flight_after(counts: dict[str, int], start: str) -> str | None:
    later = [d for d, n in counts.items() if n > 0 and d >= start]
    return min(later) if later else None


# --- The state machine -----------------------------------------------------

def detect(conn, day: date | None = None) -> dict:
    day = day or metrics.reference_day()
    cov = _coverage_map(conn)
    if not metrics.was_observed(cov, day.isoformat()):
        LOG.warning("coverage not ok for %s; suspension state left untouched", day)
        return {"opened": 0, "resumed": 0, "skipped": True,
                "opened_events": [], "resumed_events": []}

    scopes = build_scopes(conn)
    # Per airport and per fetch direction, the days that fetch actually
    # delivered. The silence walk needs it for every day it might count, and
    # it only counts days inside the scope's span.
    ap_cov = metrics.airport_side_coverage(conn, day, max(SPAN_DAYS.values()))
    # ...and whether each end of a route could be named at all.
    nameable = metrics.nameable_days(conn, day, max(SPAN_DAYS.values()))
    # ...and whether this feed carries the operator at all. The last_flight_on
    # requirement below already refuses a stop for a carrier never once seen,
    # which is what saved the ledger from these on 2026-08-17. It stops being
    # enough the moment one stray leg gives such a carrier a last operating
    # day: British Airways has exactly one in the record, Heathrow to Amman on
    # 2026-08-23, against 63.7 departures a week of baseline it never appears
    # on. Ask the question directly rather than rely on that.
    car_vis = metrics.carrier_visibility(conn, day, config.CARRIER_VISIBILITY_DAYS)
    opened: list[dict] = []
    resumed: list[dict] = []

    for scope, entries in scopes.items():
        limit = THRESHOLD[scope]
        floor = MIN_BASELINE[scope]
        span = SPAN_DAYS[scope]
        for key, meta in entries.items():
            if meta["baseline"] < floor:
                continue
            if (car_vis.get(meta["carrier"], 1.0)
                    < config.MIN_CARRIER_VISIBILITY):
                continue
            counts = _daily_counts(conn, scope, key)
            s = silence(counts, cov, day, span,
                        visible_days(scope, key, meta, ap_cov, nameable))

            active = conn.execute(
                "SELECT * FROM suspension WHERE scope=? AND scope_key=? AND status='active'",
                (scope, key)).fetchone()

            # --- resumption ------------------------------------------------
            if s["silent_days"] == 0 and active:
                back = first_flight_after(counts, active["started_on"]) or day.isoformat()
                # A resumption cannot predate the stop it ends. That only
                # happens when the event and the traffic behind it have drifted
                # apart (a rebuilt daily_route, a restored db). Clamp rather
                # than publish a negative duration.
                back = max(back, active["started_on"])
                stopped_for = (_d(back) - _d(active["started_on"])).days
                conn.execute(
                    """UPDATE suspension SET status='resumed', resumed_on=?,
                       days_stopped=? WHERE id=?""",
                    (back, stopped_for, active["id"]))
                resumed.append({"scope": scope, "carrier": meta["carrier"],
                                "detail": meta["detail"], "resumed_on": back,
                                "days_stopped": stopped_for})
                LOG.info("RESUMED %s %s after %s days", scope, key, stopped_for)
                continue

            # --- still stopped: keep the duration current -------------------
            if active:
                conn.execute(
                    "UPDATE suspension SET days_stopped=? WHERE id=?",
                    ((day - _d(active["started_on"])).days, active["id"]))
                continue

            # --- new stop ---------------------------------------------------
            #
            # A scope we have never once seen operating cannot have stopped:
            # there is no day to date the stop from, and the honest reading of
            # a baseline route with no sighting is that our receivers do not
            # cover it. British Airways held five of the 147 stops opened on
            # 2026-08-17, including London-Dubai at 18 departures a week, on
            # zero observed legs in the entire record. The old fallback here
            # took `day - silent_days` -- subtracting a count of OBSERVED days
            # as if they were CALENDAR days -- and published 2026-08-09 as the
            # start, a day whose own coverage row reads `outage`, while the
            # feed's reading_note promised "started_on is the first silent
            # day". Withhold instead; the ratio column already reports these
            # carriers as never observed.
            if s["silent_days"] >= limit and s["last_flight_on"]:
                started = (_d(s["last_flight_on"]) + timedelta(days=1)).isoformat()
                # One more falsification before we say it out loud.
                back = reverse_flew(conn, scope, key, started)
                if back:
                    LOG.info("withheld %s %s: reverse leg flew %s", scope, key, back)
                    continue
                seen = board_flew(conn, scope, key, started)
                if seen:
                    LOG.info("withheld %s %s: board listed its own aircraft %s",
                             scope, key, seen)
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO suspension
                       (scope, scope_key, carrier, detail, baseline_weekly,
                        last_flight_on, started_on, detected_on, days_stopped,
                        status, confidence)
                       VALUES (?,?,?,?,?,?,?,?,?, 'active', 'observed')""",
                    (scope, key, meta["carrier"], meta["detail"],
                     round(meta["baseline"], 1), s["last_flight_on"], started,
                     day.isoformat(), (day - _d(started)).days))
                opened.append({"scope": scope, "carrier": meta["carrier"],
                               "detail": meta["detail"], "started_on": started,
                               "days_stopped": (day - _d(started)).days,
                               "baseline_weekly": round(meta["baseline"], 1)})
                LOG.info("STOPPED %s %s since %s", scope, key, started)

    withdrawn = withdraw_contradicted(conn)
    conn.commit()
    return {"opened": len(opened), "resumed": len(resumed), "skipped": False,
            "withdrawn": withdrawn,
            "opened_events": opened, "resumed_events": resumed}


def withdraw_contradicted(conn) -> int:
    """Drop active stops the boards contradict. Returns how many.

    A separate pass over the ledger, and it has to be: the loop above only
    revisits scopes it still derives from the baseline and the silence
    computation, so a stop whose scope later falls out of that set -- the
    carrier loses its baseline row, or a visibility gate skips it -- is never
    looked at again. That is the mechanism behind "detection never clears its
    own false stops", which is why the 147 of 2026-08-17 and the 9 of
    2026-08-20 both had to be deleted by hand.

    Withdrawn, not resumed. `resumed` asserts that service stopped and came
    back; saying that about a carrier which never stopped is a second false
    claim laid over the first. The row stays for the audit trail, and
    `report()` reads neither status, so it leaves the page.
    """
    n = 0
    for row in conn.execute(
            "SELECT id, scope, scope_key, started_on FROM suspension "
            "WHERE status='active'").fetchall():
        seen = board_flew(conn, row["scope"], row["scope_key"], row["started_on"])
        if not seen:
            continue
        conn.execute("UPDATE suspension SET status='withdrawn' WHERE id=?",
                     (row["id"],))
        LOG.info("WITHDREW %s %s: board listed its own aircraft %s",
                 row["scope"], row["scope_key"], seen)
        n += 1
    return n


# --- Reporting -------------------------------------------------------------

def report(conn, resumed_window_days: int = 30) -> dict:
    carriers = config.carriers()
    active = [dict(r) for r in conn.execute(
        """SELECT * FROM suspension WHERE status='active'
           ORDER BY CASE scope WHEN 'region' THEN 0 WHEN 'station' THEN 1 ELSE 2 END,
                    baseline_weekly DESC""")]
    cutoff = (metrics.today_utc() - timedelta(days=resumed_window_days)).isoformat()
    recent = [dict(r) for r in conn.execute(
        "SELECT * FROM suspension WHERE status='resumed' AND resumed_on >= ? "
        "ORDER BY resumed_on DESC", (cutoff,))]

    regions = {s["carrier"] for s in active if s["scope"] == "region"}
    stations = {(s["carrier"], s["detail"]) for s in active if s["scope"] == "station"}

    for s in active + recent:
        s["carrier_name"] = carriers.get(s["carrier"], {}).get("name", s["carrier"])
        s["evidence"] = [dict(e) for e in conn.execute(
            "SELECT source, title, url, published, stance FROM evidence "
            "WHERE suspension_id=? ORDER BY published DESC LIMIT 5", (s["id"],))]
        # Roll the hierarchy up. If a carrier has left the region entirely,
        # listing each airport and route it also left is noise.
        if s["scope"] == "region":
            s["superseded_by"] = None
        elif s["scope"] == "station":
            s["superseded_by"] = "region" if s["carrier"] in regions else None
        else:
            dep, _, arr = s["detail"].partition("-")
            s["superseded_by"] = (
                "region" if s["carrier"] in regions
                else "station" if ((s["carrier"], dep) in stations
                                   or (s["carrier"], arr) in stations)
                else None)

    return {
        "active": active,
        "recently_resumed": recent,
        "summary": {
            "carriers_with_any_stop": len({s["carrier"] for s in active}),
            "region_stops": sum(1 for s in active if s["scope"] == "region"),
            "station_stops": sum(1 for s in active if s["scope"] == "station"),
            "route_stops": sum(1 for s in active if s["scope"] == "route"),
            "resumed_last_30d": len(recent),
        },
    }
