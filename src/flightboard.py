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
from datetime import date, datetime, timedelta, timezone

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


# Verdicts that mean the board came back with far less than this airport
# normally has. `_verdict` sets them; what they MEAN is decided below.
COLLAPSED = ("empty", "thin")

# Consecutive collapsed board days before the collapse is reported as a
# finding about the airport rather than as something to keep watching. One day
# is not enough: this endpoint is undocumented and a single airport can come
# back empty for a few hours of its own. Two days is where a reading gets to
# be called a finding, and the first day is still published -- as provisional.
# The project's whole error history is confidently wrong output on day one.
COLLAPSE_DAYS = 2

# A board this small cannot carry a verdict about an airport. `_verdict` only
# refuses a median of zero, which lets Tehran through at a median of 1.0 --
# and "OIIE operating normally" on two listings is precisely the confidently
# wrong output this project keeps having to retract. Measured 2026-09-06, a
# floor of 10 excludes exactly one airport, and it is the one neither ADS-B
# (0 records in seven days) nor the board can see. Saying so is the answer.
MIN_BOARD_MEDIAN = 10


def _collapse_streak(conn, icao: str, day: str) -> int:
    """Consecutive board days ending at `day` that came back collapsed.

    A day with no probe row breaks the streak rather than being skipped over.
    That is deliberately the conservative direction: a day the sweep never
    reached is not evidence that the airport was quiet on it, and joining two
    collapses across a hole would manufacture a finding out of our own gap.
    """
    streak, expect = 0, date.fromisoformat(day)
    for r in conn.execute(
            "SELECT day, verdict FROM board_probe WHERE airport = ? AND day <= ? "
            "ORDER BY day DESC LIMIT 14", (icao, day)):
        if (date.fromisoformat(r["day"]) != expect
                or r["verdict"] not in COLLAPSED):
            break
        streak += 1
        expect -= timedelta(days=1)
    return streak


def airport_operation(conn, day: str | None = None) -> list[dict]:
    """Is this airport operating, according to its own published board?

    The question the project exists to answer for the airports ADS-B cannot
    see -- Riyadh, Jeddah, Kuwait, Abha, Baghdad, Erbil all return zero
    flights to every receiver, and the board is the only witness they have.
    Until now the board was read defensively only: it could withdraw a stop
    somebody else opened, and could not say anything on its own.

    What makes a collapse readable at all is the rest of the sweep. The
    realistic failure of this source is not an error but a cheerful empty
    list, and that failure is IP-wide -- so a day on which no airport at all
    scored `ok` is our scraper, and nothing on it may be read as an airport.
    A day on which fourteen airports answered normally and one came back
    empty is a statement about that one. No threshold is invented for this:
    the discriminator is whether the source demonstrably answered somebody.

    `state` is None exactly when `withheld_reason` is set, the same contract
    the carrier ratios keep, and the reasons come from the same vocabulary.
    """
    if not day:
        row = conn.execute("SELECT MAX(day) d FROM board_probe").fetchone()
        day = row["d"] if row else None
    if not day:
        return []

    probes = {r["airport"]: r for r in conn.execute(
        "SELECT airport, flights, median, verdict FROM board_probe WHERE day = ?",
        (day,))}
    source_ok = any(p["verdict"] == "ok" for p in probes.values())

    out = []
    for icao in board_airports():
        p = probes.get(icao)
        state = reason = None
        streak = 0
        if p is None:
            # No row at all: every window of the sweep failed here, and
            # sample() deliberately records nothing rather than a zero.
            reason = "no_source"
        elif not source_ok:
            # Nobody answered normally today. Fifteen airports do not close
            # together; a scraper does.
            reason = "no_source"
        elif p["verdict"] == "unproven":
            reason = "below_min_observed_days"
        elif (p["median"] or 0) < MIN_BOARD_MEDIAN:
            # There is a source; there is not enough of it to divide by.
            reason = "no_source"
        elif p["verdict"] == "ok":
            state = "normal"
        else:
            state = "stopped" if p["verdict"] == "empty" else "reduced"
            streak = _collapse_streak(conn, icao, day)
        out.append({
            "airport": icao,
            "day": day,
            "flights": p["flights"] if p else None,
            "median": p["median"] if p else None,
            "verdict": p["verdict"] if p else None,
            "state": state,
            # True while the collapse is one day old: published, and published
            # as not yet a finding.
            "provisional": bool(state in ("stopped", "reduced")
                                and streak < COLLAPSE_DAYS),
            "days": streak,
            "withheld_reason": reason,
        })
    return out


# Listings a day a carrier must normally post before its absence is worth
# reading. One or two is a codeshare tail or a weekly charter, and its gaps
# are not evidence of anything.
MIN_BOARD_LISTINGS = 2


def own_metal_since(conn) -> str | None:
    """The first day the board distinguished an operator from a ticket.

    `operated_by` was added to a table that already held weeks of rows, and
    NULL there means two different things on either side of that day: before
    it, "nobody told us"; after it, "this carrier operates the flight". The
    whole 2503-rows-a-day codeshare layer at Jeddah and Riyadh reads as own
    metal on the early days and correctly as somebody else's on the later
    ones.

    Measured 2026-09-08, and it is why this function exists rather than a
    date constant: a presence baseline drawn across that day reported American
    Airlines, British Airways, China Eastern, Iberia, JAL and Finnair as
    having all abandoned Jeddah and Riyadh on the same morning -- fifteen
    findings, five airports, one cause, and the cause was ours.
    """
    row = conn.execute(
        "SELECT MIN(day) d FROM board_flight WHERE operated_by IS NOT NULL"
    ).fetchone()
    return row["d"] if row else None


def _readable_days(conn, icao: str, day: str, window: int) -> list[str]:
    """Board days at this airport that can be compared with each other.

    Healthy at this airport, on a day the source answered somebody, and late
    enough that `operated_by` means what it says.
    """
    floor = own_metal_since(conn)
    if not floor:
        return []
    source_days = {r["day"] for r in conn.execute(
        "SELECT day FROM board_probe WHERE verdict = 'ok' GROUP BY day")}
    days = [r["day"] for r in conn.execute(
        "SELECT day FROM board_probe WHERE airport = ? AND verdict = 'ok' "
        "AND day <= ? AND day >= ? ORDER BY day DESC LIMIT ?",
        (icao, day, floor, window))]
    return sorted(d for d in days if d in source_days)


def carrier_absence(conn, day: str | None = None, window: int = 21) -> dict:
    """Tracked carriers that have vanished from a board that is still healthy.

    The other half of the airport question, for the same six airports no
    receiver reaches: not "is Riyadh running" but "who stopped running there".
    The board is the only source that can answer it, because it names the
    operator -- ADS-B resolves an aircraft and cannot tell "British Airways is
    not flying" from "we cannot see British Airways".

    What makes an absence readable is that everything around it is not: the
    airport's own board scored `ok` on every day of the absence, so the source
    was answering and this carrier is missing from an answer that arrived.

    Four guards, and every one of them is a mistake this project has already
    made somewhere else:

    * The baseline may not cross `own_metal_since()` -- see that docstring.
    * The carrier must have posted its own metal on EVERY readable day of the
      baseline, which is what the board's daily operators actually do: of 166
      airport-carrier pairs measured 2026-09-06, 156 have no gap at all.
      A carrier that already comes and goes cannot go missing.
    * ADS-B outranks the board. If a receiver saw the aeroplane at that
      airport during the absence, there is no absence.
    * One day is provisional, two is a finding, exactly as for the airport
      itself -- and for the same reason.

    Returns the findings AND how far back it could look, because "nobody
    stopped" and "we cannot see far enough back to tell" are different
    answers and an empty list says both.
    """
    if not day:
        row = conn.execute("SELECT MAX(day) d FROM board_probe").fetchone()
        day = row["d"] if row else None
    if not day:
        return {"day": None, "comparable_since": None,
                "findings": [], "readable_days": 0,
                "required_days": config.MIN_SIGNAL_HISTORY_DAYS,
                "withheld_reason": "no_source"}

    healthy = {a["airport"] for a in airport_operation(conn, day)
               if a["state"] == "normal"}
    tracked = set(config.tracked_carriers())
    out = []
    reach = 0
    for icao in sorted(healthy):
        days = _readable_days(conn, icao, day, window)
        reach = max(reach, len(days))
        if len(days) <= config.MIN_SIGNAL_HISTORY_DAYS:
            continue
        listings: dict[str, dict[str, int]] = {}
        for r in conn.execute(
                """SELECT carrier, day, COUNT(*) n FROM board_flight
                   WHERE airport = ? AND operated_by IS NULL
                     AND day BETWEEN ? AND ?
                   GROUP BY carrier, day""", (icao, days[0], days[-1])):
            if r["carrier"] in tracked:
                listings.setdefault(r["carrier"], {})[r["day"]] = r["n"]

        for carrier, byday in listings.items():
            gone = 0
            for d in reversed(days):
                if byday.get(d):
                    break
                gone += 1
            if not gone:
                continue
            base = days[:len(days) - gone]
            if len(base) < config.MIN_SIGNAL_HISTORY_DAYS:
                continue
            if not all(byday.get(d) for d in base):
                continue
            mean = sum(byday[d] for d in base) / len(base)
            if mean < MIN_BOARD_LISTINGS:
                continue
            first_absent = days[len(days) - gone]
            seen = conn.execute(
                """SELECT MAX(dep_date) d FROM flight
                   WHERE carrier = ? AND dep_date >= ?
                     AND (dep_icao = ? OR arr_icao = ?)""",
                (carrier, first_absent, icao, icao)).fetchone()
            if seen and seen["d"]:
                # A sighting outranks a listing, here as everywhere.
                continue
            out.append({
                "airport": icao,
                "carrier": carrier,
                "last_listed": base[-1],
                "days": gone,
                "provisional": gone < COLLAPSE_DAYS,
                "listings_before": round(mean, 1),
                "baseline_days": len(base),
            })
    reason = None
    if not healthy:
        reason = "no_source"
    elif reach <= config.MIN_SIGNAL_HISTORY_DAYS:
        # Not "nobody stopped": nobody could be told apart yet. The window
        # that counts starts at own_metal_since(), so this reads short for
        # days after that day, not after the boards began.
        reason = "below_min_observed_days"
    return {"day": day, "comparable_since": own_metal_since(conn),
            "findings": sorted(
                out, key=lambda r: (-r["days"], r["airport"], r["carrier"])),
            "readable_days": reach,
            "required_days": config.MIN_SIGNAL_HISTORY_DAYS,
            "withheld_reason": reason}


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
