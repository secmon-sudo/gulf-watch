"""Keyless ADS-B aggregators, used as OpenSky failover and for FIR sampling.

adsb.lol       -- no key, ODbL 1.0, ADSBExchange-v2 compatible response shape.
airplanes.live -- no key, 1 request/second, non-commercial.
adsb.fi        -- no key, open data. Third in line, added 2026-08-12.

All volunteer networks with no SLA. We try them in order and fall through.
Attribution is emitted in every published JSON payload (see publish.py) because
ODbL requires it.

A third backend buys resilience, not coverage, and the difference matters
enough to write down. Measured 2026-08-12 at one instant across all three:
Dubai 130 / 117 / 133 aircraft, Riyadh 17 / 40 / 18, and Jeddah, Tehran and
Baghdad flat zero on every one of them at both 250 nm and 40 nm. They read the
same volunteer receivers, so no aggregator opens a blind airport -- what this
buys is that two of them failing no longer produces an empty list, which
firwatch cannot tell apart from empty airspace.

adsb.fi was written off on 2026-08-05 as returning nothing over Dubai. That was
a measurement bug, not a property of the source: its point query lives at
/lat/{lat}/lon/{lon}/dist/{d} rather than /point/..., which answers 400, and it
keys that response `aircraft` where every other backend and its own /callsign
endpoint say `ac`. Both shapes are handled below.

adsb.one was tested at the same time and is unusable: HTTP 403 with a
Cloudflare challenge on every request, under a plain and a browser User-Agent
alike. adsbexchange has a genuinely different coverage policy but its community
API needs a paid key, so it has never been measured here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .config import MIN_NIC, MIN_SIL

LOG = logging.getLogger("gulfwatch.adsb")

# (name, base, point-path template). The callsign path is uniform.
BACKENDS = [
    ("adsblol", "https://api.adsb.lol/v2", "/point/{lat}/{lon}/{dist}"),
    ("airplaneslive", "https://api.airplanes.live/v2", "/point/{lat}/{lon}/{dist}"),
    ("adsbfi", "https://opendata.adsb.fi/api/v2", "/lat/{lat}/lon/{lon}/dist/{dist}"),
]

_session = requests.Session()
_session.headers["User-Agent"] = "gulfwatch/1.0 (personal research)"

# airplanes.live asks for one request a second and adsb.lol throttles a burst.
# The spacing used to sit at the bottom of the fallback loop, which meant it
# only ran after a backend had already failed -- consecutive successful calls
# went out back to back. Measured on the ingests of 2026-08-10: three adsb.lol
# calls 110-153 ms apart, then adsb.lol 429ing on exactly 6 of the 12 FIR
# points on both runs, so half the FIR sample silently came from the fallback.
#
# That mattered more than a slow sample: when both backends are exhausted
# _get() returns an empty list, and firwatch writes that as a FIR with no
# carriers, which is indistinguishable from empty airspace. Pacing each backend
# on its own clock also lets a fallback fire immediately instead of waiting out
# a sleep that bought nothing.
MIN_INTERVAL = 1.1
_last_call: dict[str, float] = {}


def _pace(name: str) -> None:
    """Hold MIN_INTERVAL between two requests to the same backend."""
    wait = MIN_INTERVAL - (time.monotonic() - _last_call.get(name, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[name] = time.monotonic()


def _aircraft(payload: dict) -> list[dict]:
    """adsb.fi keys its point response `aircraft`; everything else says `ac`."""
    return payload.get("ac") or payload.get("aircraft") or []


def _get(paths: dict[str, str]) -> tuple[str, list[dict]]:
    """Try each backend in order. `paths` maps backend name -> path."""
    for name, base, _ in BACKENDS:
        _pace(name)
        path = paths[name]
        try:
            resp = _session.get(f"{base}{path}", timeout=30)
            if resp.status_code == 200:
                return name, _aircraft(resp.json())
            LOG.warning("%s returned %s for %s", name, resp.status_code, path)
        except requests.RequestException as exc:
            LOG.warning("%s failed: %s", name, exc)
    return "none", []


def point(lat: float, lon: float, radius_nm: int = 250) -> tuple[str, list[dict]]:
    """Live aircraft within radius_nm of a point. radius is capped at 250 nm."""
    dist = min(radius_nm, 250)
    return _get({name: tmpl.format(lat=lat, lon=lon, dist=dist)
                 for name, _, tmpl in BACKENDS})


def callsign(cs: str) -> tuple[str, list[dict]]:
    path = f"/callsign/{cs.upper()}"
    return _get({name: path for name, _, _ in BACKENDS})


def position_is_trustworthy(ac: dict[str, Any]) -> bool:
    """Reject spoofed/jammed positions.

    The Gulf and Levant see sustained GNSS interference. Aircraft under
    spoofing still transmit ADS-B, but with degraded integrity figures --
    NIC (navigation integrity category) and SIL (source integrity level)
    collapse. Counting those positions as real overflights produces
    confident nonsense, so anything below the floor is dropped.
    """
    if ac.get("lat") is None or ac.get("lon") is None:
        return False
    nic = ac.get("nic")
    sil = ac.get("sil")
    if nic is not None and nic < MIN_NIC:
        return False
    if sil is not None and sil < MIN_SIL:
        return False
    # Ground-only or obviously bogus altitude
    alt = ac.get("alt_baro")
    if isinstance(alt, (int, float)) and alt < 0:
        return False
    return True
