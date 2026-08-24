"""Callsign -> carrier resolution.

ADS-B callsigns are 8 characters, space padded, e.g. "QTR8   ", "PGT751 ".
The first three letters are the operator's ICAO designator. This is *not* the
IATA code and it is the single most common place these projects go wrong:
flynas is KNE not XY, Pegasus is PGT not PC, Saudia is SVA not SV.

Caveats that are deliberately visible in the output rather than hidden:
  * Codeshares are invisible to ADS-B. A QR-marketed flight operated by an
    ACMI partner shows the partner's callsign. We count the operator.
  * Wet leases show the lessor's callsign for the same reason.
  * Freighters share the passenger callsign for several carriers, so we split
    on flight-number range (configured per carrier).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Three letters, a flight number, and up to TWO trailing letters. One was not
# enough and the shortfall was invisible from inside our own data, because a
# record dropped here was never stored: measured against the live API on
# 2026-08-24, `^([A-Z]{3})(\d{1,4}[A-Z]?)$` threw away 74 of 439 arrivals into
# Dubai for 2026-08-19 -- 43 flydubai, 25 Emirates, 3 Turkish -- on shapes like
# UAE4DT, FDB7ZK and THY9ZD. That is 17% of the day, concentrated on the two
# carriers whose observed-versus-timetable ratio has read impossibly low.
#
# Still rejected, and deliberately: DPDB205 and ABCDEF (the suffix must start
# with a digit) and N123AB (a registration, not a callsign).
CALLSIGN_RE = re.compile(r"^([A-Z]{3})(\d{1,4}[A-Z]{0,2})$")


def parse_callsign(raw: str | None) -> tuple[str | None, int | None]:
    """Return (carrier_icao, flight_number) or (None, None)."""
    if not raw:
        return None, None
    cs = raw.strip().upper()
    m = CALLSIGN_RE.match(cs)
    if not m:
        return None, None
    prefix, num = m.group(1), m.group(2)
    # All of the trailing letters, not one of them. Stripping a single letter
    # off "4DT" leaves "4D", which int() rejects, so widening the pattern above
    # without widening this would have parsed the carrier and thrown the flight
    # number away -- and the flight number is what tells a freighter from a
    # passenger leg.
    digits = re.sub(r"[A-Z]+$", "", num)
    try:
        return prefix, int(digits)
    except ValueError:
        return prefix, None


def is_freight(carrier_cfg: dict, flight_number: int | None) -> bool:
    if carrier_cfg.get("freight_only"):
        return True
    floor = carrier_cfg.get("cargo_flightnum_min")
    if floor and flight_number and flight_number >= floor:
        return True
    return False


def normalise(record: dict, carriers: dict, source: str) -> dict | None:
    """Turn one OpenSky flight object into a flight table row."""
    carrier, number = parse_callsign(record.get("callsign"))
    if carrier is None or carrier not in carriers:
        return None
    cfg = carriers[carrier]
    first_seen = record.get("firstSeen")
    if not first_seen:
        return None
    day = datetime.fromtimestamp(first_seen, tz=timezone.utc).strftime("%Y-%m-%d")
    return {
        "icao24": (record.get("icao24") or "").lower(),
        "first_seen": int(first_seen),
        "last_seen": record.get("lastSeen"),
        "callsign": (record.get("callsign") or "").strip(),
        "carrier": carrier,
        "flight_number": number,
        "dep_icao": record.get("estDepartureAirport"),
        "arr_icao": record.get("estArrivalAirport"),
        "is_freight": int(is_freight(cfg, number)),
        "dep_date": day,
        "source": source,
        "ingested_at": int(datetime.now(tz=timezone.utc).timestamp()),
    }
