"""Keyless ADS-B aggregators, used as OpenSky failover and for FIR sampling.

adsb.lol      -- no key, ODbL 1.0, ADSBExchange-v2 compatible response shape.
airplanes.live -- no key, 1 request/second, non-commercial.

Both are volunteer networks with no SLA. We try adsb.lol first and fall through.
Attribution is emitted in every published JSON payload (see publish.py) because
ODbL requires it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .config import MIN_NIC, MIN_SIL

LOG = logging.getLogger("gulfwatch.adsb")

BACKENDS = [
    ("adsblol", "https://api.adsb.lol/v2"),
    ("airplaneslive", "https://api.airplanes.live/v2"),
]

_session = requests.Session()
_session.headers["User-Agent"] = "gulfwatch/1.0 (personal research)"


def _get(path: str) -> tuple[str, list[dict]]:
    for name, base in BACKENDS:
        try:
            resp = _session.get(f"{base}{path}", timeout=30)
            if resp.status_code == 200:
                return name, resp.json().get("ac") or []
            LOG.warning("%s returned %s for %s", name, resp.status_code, path)
        except requests.RequestException as exc:
            LOG.warning("%s failed: %s", name, exc)
        time.sleep(1.1)
    return "none", []


def point(lat: float, lon: float, radius_nm: int = 250) -> tuple[str, list[dict]]:
    """Live aircraft within radius_nm of a point. radius is capped at 250 nm."""
    return _get(f"/point/{lat}/{lon}/{min(radius_nm, 250)}")


def callsign(cs: str) -> tuple[str, list[dict]]:
    return _get(f"/callsign/{cs.upper()}")


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
