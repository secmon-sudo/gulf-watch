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

    cutoff = (datetime.now(tz=timezone.utc)
              - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    fresh = {(r["dep_iata"], r["arr_iata"]) for r in conn.execute(
        "SELECT dep_iata, arr_iata FROM schedule_probe WHERE fetched_at > ?",
        (cutoff,))}

    todo = [p for p in pairs() if p not in fresh]
    if limit:
        todo = todo[:limit]
    LOG.info("%s pairs to refresh (%s still fresh)", len(todo), len(fresh))

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

    return {"fetched": fetched, "skipped": len(fresh), "no_key": False}


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
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT carrier, dep_iata, arr_iata, weekly, codeshare "
            "FROM route_schedule WHERE weekly > 0"):
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
    return {"pairs_probed": probes["n"], "pairs_total": len(pairs()),
            "last_fetched": probes["last"]}
