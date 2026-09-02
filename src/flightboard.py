"""Arrival/departure boards, read for the operator ADS-B cannot name.

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

# Twelve-hour windows, two of them, so a full UTC day is covered per direction.
# Six-hour windows were 56 requests a day across seven airports and ran for
# weeks without complaint; at fifteen airports that became 120 and flightstats
# answered 403 from about the twenty-fourth onwards, an IP-wide block that then
# refused even the airports which had worked all along. This halves it back to
# 60, inside the range already proven safe.
#
# Verified lossless before it shipped, because a wider window that quietly
# returns less would trade a rate problem for a data problem: for Dubai
# departures on 2026-09-01 the two six-hour windows at 0 and 6 hold 257 and 402
# flights, 597 once their 62 shared rows collapse, and the single twelve-hour
# window holds exactly those 597 -- nothing missing either way.
WINDOW_HOURS = 12
WINDOW_STARTS = [0, 12]

# Seconds between requests. Was 1.0 for seven airports.
REQUEST_DELAY = 2.0

# Consecutive failures that end the whole sweep. Without this the walk answered
# a block by sending the remaining ninety-six requests into it, which is both
# useless and the surest way to make the block longer. Stopping early costs one
# day of boards; being banned costs every day until it lifts.
ABORT_AFTER = 8

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


def board_airports() -> dict[str, dict]:
    """Every monitored airport, keyed by ICAO.

    This used to be the ADS-B-blind seven only, on the reasoning that the
    other eight already had a witness. That was wrong, and it cost the project
    its central question. ADS-B identifies an aircraft, not an operator: the
    thirteen carriers that vanished from the feed in 2026-08 were read as our
    own blind spot for three weeks, because nothing we had could tell "British
    Airways is not flying" from "we cannot see British Airways". The board can
    -- it names the operator -- but it was only ever pointed at seven airports
    that British Airways has never served.

    Measured 2026-09-02, once it was pointed at Dubai: 1097 departure listings,
    twenty-one of them to Heathrow, every one operated by Emirates, and no BA
    metal anywhere. That is the observation the whole thing was built to make.
    """
    return dict(config.airports())


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
                    now,
                    # The board lists a codeshare under the carrier whose
                    # number is on the ticket, so reading `carrier` alone says
                    # British Airways serves Riyadh when the aeroplane is
                    # Qatar's. Measured 2026-09-02: of BA's 253 entries across
                    # the seven blind airports, 253 were Doha codeshares and
                    # none was BA metal. 43% of Dubai's board carries this.
                    f.get("operatedBy")))
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
    misses = 0
    for icao, cfg in board_airports().items():
        if misses >= ABORT_AFTER:
            LOG.warning("%s and the rest skipped: %s requests in a row failed, "
                        "so the source is refusing us and hammering it would "
                        "only prolong that", icao, misses)
            flagged.append(f"{icao}:skipped")
            continue
        seen: set[tuple] = set()
        failed = False
        for direction in ("arr", "dep"):
            for hour in WINDOW_STARTS:
                flights = _fetch(cfg["iata"], direction, day, hour)
                if flights is None:
                    failed = True
                    misses += 1
                    if misses >= ABORT_AFTER:
                        break
                    continue
                misses = 0
                for row in _rows(flights, icao, direction, key, mapping, now):
                    seen.add(row)
                time.sleep(REQUEST_DELAY)
            if misses >= ABORT_AFTER:
                break

        if failed and not seen:
            # Every window errored. Recording a zero here would poison the
            # median that later days are judged against, so record nothing.
            LOG.warning("%s: every window failed -- not recording a count", icao)
            flagged.append(f"{icao}:failed")
            continue

        conn.executemany(
            """INSERT OR REPLACE INTO board_flight
               (airport, direction, day, carrier, flight_no, other_iata,
                sched_time, fetched_at, operated_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            sorted(seen, key=lambda r: tuple("" if v is None else v for v in r)))
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
