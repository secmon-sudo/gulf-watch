"""Arrival/departure boards for the airports ADS-B cannot see.

Seven of fifteen monitored airports return zero flights from every ADS-B
source we have -- OpenSky, adsb.lol and airplanes.live alike, because all
three depend on the same volunteer receivers and nobody has one on a roof in
Riyadh, Kuwait or Baghdad. Measured: 25 aircraft over Dubai, zero over the
other six at the same moment.

A published board answers the question those airports otherwise cannot:
Emirates and Iraqi Airways appearing in Baghdad arrivals every day, and then
not appearing, is the same signal as ADS-B silence -- from a different kind of
witness. It is stored separately from `flight` and labelled separately in the
report, because a board entry is a listing and a transponder return is a
sighting, and the day those two get averaged together the report starts
lying quietly.

The endpoint is undocumented: it backs flightstats.com's own tracker UI, so it
can change or vanish without notice. Everything below assumes it will, one
day, start returning nothing -- see `_verdict`.
"""

from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

from . import config

LOG = logging.getLogger("gulfwatch.flightboard")

BASE = "https://www.flightstats.com/v2/api-next/flight-tracker"

# Six-hour windows, four of them, so a full UTC day is covered per direction.
WINDOW_HOURS = 6
WINDOW_STARTS = [0, 6, 12, 18]

# Below this share of the recent median, the board is treated as broken rather
# than as evidence. 0.3 is deliberately generous: a real collapse in traffic is
# what we are hunting, so the bar for "this is the source failing" has to sit
# well under any plausible real drop.
THIN_RATIO = 0.3

# Days of history required before a zero means anything at all. With fewer,
# there is no median to compare against and the verdict is `unproven`.
MIN_HISTORY_DAYS = 3

_session = requests.Session()
_session.headers["User-Agent"] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def blind_airports() -> dict[str, dict]:
    """The monitored airports with no ADS-B coverage, keyed by ICAO."""
    from . import schedules
    blind = set(schedules.BLIND)
    return {icao: cfg for icao, cfg in config.airports().items()
            if cfg["iata"] in blind}


def _iata_to_icao() -> dict[str, str]:
    return {cfg["iata"]: code for code, cfg in config.carriers().items()
            if cfg.get("iata")}


def _fetch(iata: str, direction: str, day: datetime, hour: int) -> list[dict] | None:
    """One board window. None means the request failed -- not that it was empty."""
    url = (f"{BASE}/{direction}/{iata}/{day.year}/{day.month}/{day.day}/{hour}"
           f"?numHours={WINDOW_HOURS}")
    try:
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        LOG.warning("%s %s %s: %s", iata, direction, hour, exc)
        return None
    return (payload.get("data") or {}).get("flights") or []


def _rows(flights: list[dict], icao: str, direction: str, day: str,
          mapping: dict[str, str], now: str) -> list[tuple]:
    out = []
    for f in flights:
        car = f.get("carrier") or {}
        code = mapping.get(car.get("fs"))
        if not code:
            continue                      # not a carrier we track
        number = str(car.get("flightNumber") or "").strip()
        if not number:
            continue
        out.append((icao, direction, day, code, number,
                    (f.get("airport") or {}).get("fs"),
                    (f.get("arrivalTime") or f.get("departureTime")
                     or {}).get("time24"),
                    now))
    return out


def _verdict(conn, icao: str, day: str, n: int) -> tuple[str, float | None]:
    """Is a low count a quiet airport, or a source that has stopped answering?

    This is the whole guard. The endpoint is undocumented and unsupported; the
    realistic failure is not an error code but a cheerful empty list, which
    without this reads as "every carrier stopped serving Baghdad overnight".
    """
    prior = [r["flights"] for r in conn.execute(
        "SELECT flights FROM board_probe WHERE airport=? AND day<? "
        "ORDER BY day DESC LIMIT 28", (icao, day))]
    if len(prior) < MIN_HISTORY_DAYS:
        return "unproven", None
    median = statistics.median(prior)
    if median <= 0:
        return "unproven", median
    if n == 0:
        return "empty", median
    if n < median * THIN_RATIO:
        return "thin", median
    return "ok", median


def sample(conn, day: datetime | None = None) -> dict:
    """Pull every blind airport's board for one UTC day. Returns a summary."""
    day = day or (datetime.now(tz=timezone.utc) - timedelta(days=1))
    key = day.strftime("%Y-%m-%d")
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    mapping = _iata_to_icao()

    written = 0
    flagged: list[str] = []
    for icao, cfg in blind_airports().items():
        seen: set[tuple] = set()
        failed = False
        for direction in ("arr", "dep"):
            for hour in WINDOW_STARTS:
                flights = _fetch(cfg["iata"], direction, day, hour)
                if flights is None:
                    failed = True
                    continue
                for row in _rows(flights, icao, direction, key, mapping, now):
                    seen.add(row)
                time.sleep(1.0)

        if failed and not seen:
            # Every window errored. Recording a zero here would poison the
            # median that later days are judged against, so record nothing.
            LOG.warning("%s: every window failed -- not recording a count", icao)
            flagged.append(f"{icao}:failed")
            continue

        conn.executemany(
            """INSERT OR REPLACE INTO board_flight
               (airport, direction, day, carrier, flight_no, other_iata,
                sched_time, fetched_at) VALUES (?,?,?,?,?,?,?,?)""",
            sorted(seen))
        written += len(seen)

        verdict, median = _verdict(conn, icao, key, len(seen))
        conn.execute(
            """INSERT OR REPLACE INTO board_probe
               (airport, day, flights, median, verdict, fetched_at)
               VALUES (?,?,?,?,?,?)""",
            (icao, key, len(seen), median, verdict, now))
        conn.commit()

        if verdict in ("empty", "thin"):
            LOG.warning("%s: %s flights against a median of %s -- board marked "
                        "%s, NOT read as an absence of traffic",
                        icao, len(seen), median, verdict)
            flagged.append(f"{icao}:{verdict}")
        else:
            LOG.info("%s: %s board entries (%s)", icao, len(seen), verdict)

    return {"day": key, "written": written, "flagged": flagged}


def by_airport(conn, day: str | None = None) -> list[dict]:
    """What the boards say, with the verdict attached so a reader can weigh it."""
    if not day:
        row = conn.execute("SELECT MAX(day) d FROM board_probe").fetchone()
        day = row["d"] if row else None
    if not day:
        return []
    out = []
    for p in conn.execute(
            "SELECT airport, flights, median, verdict FROM board_probe "
            "WHERE day=? ORDER BY airport", (day,)):
        carriers = [r["carrier"] for r in conn.execute(
            "SELECT carrier, COUNT(*) n FROM board_flight "
            "WHERE airport=? AND day=? GROUP BY carrier ORDER BY n DESC",
            (p["airport"], day))]
        out.append({"airport": p["airport"], "flights": p["flights"],
                    "median": p["median"], "verdict": p["verdict"],
                    "carriers": carriers, "day": day})
    return out
