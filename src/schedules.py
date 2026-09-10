"""Published timetables from AirLabs, for the airports ADS-B cannot see.

ADS-B answers "what flew" and is blind over Kuwait, Saudi Arabia, Iraq and
Iran -- measured, seven of our fifteen airports returned zero flights. A
schedule answers a different question, "what does the carrier still intend to
fly", and it does that everywhere, because it comes from the airline rather
than from a receiver on someone's roof.

Neither replaces the other. A schedule can list a flight that is being
cancelled daily; ADS-B can miss a flight that certainly operated. The report
shows both and says which is which.

Free tier: 1000 requests a month, and the response is hard-capped at 50 rows
no matter what `limit` says, with no working `offset`. So a broad "everything
from KWI" query silently returns only destinations A through B. We therefore
ask city pair by city pair, where the answer comfortably fits, and cache it --
a timetable changes far more slowly than once a week.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from . import config

LOG = logging.getLogger("gulfwatch.schedules")

API = "https://airlabs.co/api/v9/routes"

# Airports OpenSky has no receiver coverage over, measured across a real 48h
# ingest: every one of these returned zero flights while Dubai returned 944.
BLIND = ["KWI", "RUH", "JED", "EBL", "AHB", "BGW", "IKA"]

# Airports it does see, which is where the blind ones' traffic connects to.
SEEING = ["DXB", "DOH", "SHJ", "AMM", "AUH", "BAH", "BEY", "MCT"]

DEFAULT_MAX_AGE_DAYS = 7

# Pairs with one end outside the monitored fifteen, asked about for the
# carriers no other surface can speak for. Measured 2026-09-10: eleven tracked
# carriers -- British Airways, Finnair, Iberia, JAL, American, Air China,
# China Eastern and Southern, EgyptAir, Royal Air Maroc, Air Algerie -- carry
# no board ratio (their Gulf presence is somebody else's metal wearing their
# number) and no usable ADS-B baseline. The timetable was the last hope for
# them and it could not reach them either: every one of the 722 rows in
# `route_schedule` had BOTH ends monitored, so British Airways' London-Doha
# was never asked about and could be dropped without a trace here.
#
# What this is NOT: a second observation. A timetable is a statement of
# intent, and `drops()` already says so. It is the only statement available
# for these carriers.
#
# The quota arithmetic, because there is no headroom to be casual with. The
# base sweep is 210 ordered pairs every 7 days, ~910 of the free tier's 1000
# monthly requests. These pairs are asked monthly and capped, so the worst
# case adds 30 and lands near 940. Two consequences, both deliberate: the
# cap is a constant rather than "whatever the query returns", and the base
# pairs go FIRST in the refresh, so a quota that runs out drops these and
# never the timetable the blind airports depend on.
FOREIGN_MAX_AGE_DAYS = 30
FOREIGN_MAX_PAIRS = 30

# A pair has to be a service before it can be a drop. Both floors are read
# off the boards, which is where the IATA codes come from -- `daily_route`
# holds ICAO and there is no ICAO-to-IATA map for airports outside the
# monitored fifteen.
FOREIGN_WINDOW_DAYS = 30
FOREIGN_MIN_DAYS = 3


def _key() -> str | None:
    key = os.environ.get("AIRLABS_API_KEY")
    if key:
        return key
    env = config.ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("AIRLABS_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def pairs() -> list[tuple[str, str]]:
    """Every ordered pair among the monitored airports.

    Originally only blind-to-visible, on the reasoning that ADS-B already
    covers the rest. That was right for "who serves Kuwait" and wrong for the
    question the report actually needs a denominator for: 719 observed
    departures means nothing without the number that was scheduled. Comparing
    the two requires both sides to count the same universe, so every monitored
    city pair is probed and the ratio is taken over exactly those.
    """
    every = BLIND + SEEING
    return [(a, b) for a in every for b in every if a != b]


def foreign_pairs(conn) -> list[tuple[str, str]]:
    """Routes with one foreign end, for the carriers the boards cannot rate.

    The carrier gate is the board's own publishing floor: below
    `MIN_BOARD_CARRIER_LISTINGS` a day the board withholds that carrier's
    ratio, which is precisely the set that needs a third surface. Deriving it
    rather than listing eleven codes keeps it true when a carrier crosses the
    floor in either direction.

    Direction is the monitored end outbound. A carrier removing a route
    removes both legs, so one is enough to notice, and asking both would
    double a cost that has no room to double.
    """
    from . import flightboard

    monitored = {v["iata"] for v in config.airports().values()}
    iata_of = {k: v["iata"] for k, v in config.airports().items()}
    floor = flightboard.own_metal_since(conn)
    if not floor:
        return []
    since = max(floor, (datetime.now(tz=timezone.utc)
                        - timedelta(days=FOREIGN_WINDOW_DAYS)).date().isoformat())
    days = conn.execute(
        "SELECT COUNT(DISTINCT day) d FROM board_flight WHERE day >= ?",
        (since,)).fetchone()["d"] or 1

    # Tracked carriers only. Air France clears the same floor and is a
    # control carrier -- it exists to measure the network, not to be reported
    # on -- and two of thirteen pairs went to Paris before this line.
    tracked = set(config.tracked_carriers())
    dark = {r["carrier"] for r in conn.execute(
        "SELECT carrier, COUNT(*) n FROM board_flight "
        "WHERE operated_by IS NULL AND day >= ? GROUP BY carrier", (since,))
        if r["carrier"] in tracked
        and r["n"] / days < flightboard.MIN_BOARD_CARRIER_LISTINGS}

    out = []
    for r in conn.execute(
            """SELECT airport, carrier, other_iata, COUNT(DISTINCT day) d
               FROM board_flight
               WHERE operated_by IS NULL AND day >= ? AND other_iata IS NOT NULL
               GROUP BY airport, carrier, other_iata
               ORDER BY d DESC, COUNT(*) DESC""", (since,)):
        if (r["carrier"] not in dark or r["d"] < FOREIGN_MIN_DAYS
                or r["other_iata"] in monitored):
            continue
        pair = (iata_of[r["airport"]], r["other_iata"])
        if pair not in out:
            out.append(pair)
    return out[:FOREIGN_MAX_PAIRS]


def _fetch(dep: str, arr: str, key: str) -> list[dict] | None:
    try:
        resp = requests.get(API, params={"dep_iata": dep, "arr_iata": arr,
                                         "api_key": key}, timeout=40)
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        LOG.warning("airlabs %s-%s failed: %s", dep, arr, exc)
        return None
    if body.get("error"):
        LOG.warning("airlabs %s-%s: %s", dep, arr, body["error"])
        return None
    return body.get("response") or []


def refresh(conn, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
            limit: int | None = None) -> dict:
    """Fetch any pair we have not asked about recently. Returns a summary."""
    key = _key()
    if not key:
        LOG.warning("no AIRLABS_API_KEY -- skipping the schedule refresh")
        return {"fetched": 0, "skipped": 0, "no_key": True}

    def stale(candidates, age_days):
        cutoff = (datetime.now(tz=timezone.utc)
                  - timedelta(days=age_days)).isoformat(timespec="seconds")
        fresh = {(r["dep_iata"], r["arr_iata"]) for r in conn.execute(
            "SELECT dep_iata, arr_iata FROM schedule_probe WHERE fetched_at > ?",
            (cutoff,))}
        return [p for p in candidates if p not in fresh], len(fresh)

    # Monitored pairs first. They are the timetable the blind airports depend
    # on, and if the month's quota runs out mid-sweep the foreign extras are
    # what should be missing from the end of the list.
    todo, fresh = stale(pairs(), max_age_days)
    extra, _ = stale(foreign_pairs(conn), FOREIGN_MAX_AGE_DAYS)
    todo += [p for p in extra if p not in todo]
    if limit:
        todo = todo[:limit]
    LOG.info("%s pairs to refresh, %s of them foreign (%s still fresh)",
             len(todo), len(extra), fresh)

    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    fetched = 0
    for dep, arr in todo:
        rows = _fetch(dep, arr, key)
        if rows is None:
            break               # quota gone or network down; keep what we have
        agg: dict[str, dict] = {}
        for r in rows:
            code = r.get("airline_icao")
            if not code:
                continue
            a = agg.setdefault(code, {"weekly": 0, "flights": set(), "cs": 0})
            # A marketed codeshare is somebody else's aeroplane. AirLabs lists
            # it under the airline whose number is on the ticket, and counting
            # its days as that airline's own timetable is how American Airlines
            # came to hold 220 departures a week at Jeddah and Riyadh -- Qatar
            # Airways flights wearing an AA number -- while ADS-B saw none of
            # them, which the report then published as %0.
            #
            # It is not a small correction and it is not confined to carriers
            # we cannot see: Royal Air Maroc rendered "12 sefer havada
            # görüldü" and "%0" in the same row on 2026-08-24, against a
            # denominator of 163 weekly departures it does not operate.
            #
            # The count is kept, because "this pair is mostly codeshare" is
            # worth knowing. Only the weekly total stops including them.
            if r.get("cs_flight_iata"):
                a["cs"] += 1
                continue
            a["weekly"] += len(r.get("days") or [])
            a["flights"].add(r.get("flight_iata"))

        before = {r["carrier"]: r["weekly"] for r in conn.execute(
            "SELECT carrier, weekly FROM route_schedule "
            "WHERE dep_iata=? AND arr_iata=?", (dep, arr))}
        prev = conn.execute(
            "SELECT routes FROM schedule_probe WHERE dep_iata=? AND arr_iata=?",
            (dep, arr)).fetchone()

        # An answer that empties a pair which had schedules yesterday is far
        # likelier to be the API than an airport losing every service it had.
        # It is the same cheerful-empty-list failure the boards have, and the
        # cost here is higher: the write below is a DELETE, so one bad answer
        # would erase the timetable and leave nothing to notice it by. Keep
        # what we hold and let the probe record the emptiness -- a second
        # consecutive empty answer is believed, because then `prev` is 0.
        wholesale = bool(before) and not agg and (prev is None or prev["routes"])
        if wholesale:
            LOG.warning("%s-%s came back empty while holding %s carriers -- "
                        "keeping the timetable; a second empty answer will "
                        "clear it", dep, arr, len(before))
        else:
            conn.execute(
                "DELETE FROM route_schedule WHERE dep_iata=? AND arr_iata=?",
                (dep, arr))
            conn.executemany(
                """INSERT INTO route_schedule
                   (dep_iata, arr_iata, carrier, weekly, flights, codeshare,
                    fetched_at) VALUES (?,?,?,?,?,?,?)""",
                [(dep, arr, c, v["weekly"], len(v["flights"]), v["cs"], now)
                 for c, v in agg.items()])
            # What changed, kept because the table above cannot remember. A
            # carrier that drops a route here is recorded against a pair where
            # somebody else survived -- the wholesale case never reaches this.
            day = now[:10]
            changes = []
            for carrier, was in before.items():
                is_now = agg.get(carrier, {}).get("weekly", 0)
                if is_now != was:
                    changes.append((dep, arr, carrier, day, was, is_now))
            for carrier, v in agg.items():
                if carrier not in before:
                    changes.append((dep, arr, carrier, day, None, v["weekly"]))
            if changes:
                conn.executemany(
                    """INSERT OR REPLACE INTO schedule_change
                       (dep_iata, arr_iata, carrier, day, weekly_before,
                        weekly_after) VALUES (?,?,?,?,?,?)""", changes)

        conn.execute(
            """INSERT OR REPLACE INTO schedule_probe
               (dep_iata, arr_iata, routes, fetched_at) VALUES (?,?,?,?)""",
            (dep, arr, len(rows), now))
        conn.commit()
        fetched += 1
        LOG.info("%s-%s: %s routes, %s carriers", dep, arr, len(rows), len(agg))

    return {"fetched": fetched, "skipped": fresh, "no_key": False}


def drops(conn, days: int = 30) -> list[dict]:
    """Routes carriers removed from their own published timetable.

    The closest thing this project has to a statement of intent: ADS-B
    silence and a missing board listing are both observations of an absence,
    while a timetable that no longer carries the route is the airline saying
    so. It was invisible until `schedule_change` existed, because
    `route_schedule` is rewritten in place and a dropped route simply stopped
    being a row.

    Only drops to zero, and only against a pair where other carriers
    survived: refresh() never records a change for a pair that emptied
    wholesale, so everything returned here stood beside somebody still
    flying.
    """
    since = (datetime.now(tz=timezone.utc)
             - timedelta(days=days)).date().isoformat()
    return [dict(r) for r in conn.execute(
        """SELECT dep_iata, arr_iata, carrier, day, weekly_before
           FROM schedule_change
           WHERE day >= ? AND weekly_after = 0 AND weekly_before > 0
           ORDER BY day DESC, weekly_before DESC""", (since,))]


def changes_since(conn) -> str | None:
    """The first day the timetable's changes were recorded at all.

    Worth printing beside an empty drop list: this table starts when it was
    added, so "no drops" before that day means nothing was watching, not that
    nothing moved.
    """
    row = conn.execute("SELECT MIN(day) d FROM schedule_change").fetchone()
    return row["d"] if row else None


def by_carrier(conn) -> dict[str, dict]:
    """carrier -> {airports: {iata: weekly}, weekly: total, codeshare: bool}."""
    # Monitored-to-monitored only. `weekly` here is a denominator the page
    # prints beside observed legs, and those legs are counted at monitored
    # airports; letting a foreign-end row into the sum would compare a wider
    # timetable against a narrower observation and read as a cut.
    monitored = {v["iata"] for v in config.airports().values()}
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT carrier, dep_iata, arr_iata, weekly, codeshare "
            "FROM route_schedule WHERE weekly > 0"):
        if r["dep_iata"] not in monitored or r["arr_iata"] not in monitored:
            continue
        e = out.setdefault(r["carrier"], {"airports": {}, "weekly": 0,
                                          "codeshare": False})
        # Credit the blind airport -- that is the one we could not otherwise
        # report on, and the reason we spent a request here.
        for iata in (r["dep_iata"], r["arr_iata"]):
            if iata in BLIND:
                e["airports"][iata] = e["airports"].get(iata, 0) + r["weekly"]
        e["weekly"] += r["weekly"]
        e["codeshare"] = e["codeshare"] or bool(r["codeshare"])
    return out


def coverage(conn) -> dict:
    probes = conn.execute(
        "SELECT COUNT(*) n, MAX(fetched_at) last FROM schedule_probe").fetchone()
    return {"pairs_probed": probes["n"],
            "pairs_total": len(pairs()) + len(foreign_pairs(conn)),
            "last_fetched": probes["last"]}
