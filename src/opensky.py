"""OpenSky Network client.

OpenSky retired username/password auth in March 2026 -- this uses the OAuth2
client-credentials flow. Tokens live ~30 minutes and are cached in memory.

Set OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET. Without them the client falls
back to anonymous access, which works but is heavily rate limited.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

LOG = logging.getLogger("gulfwatch.opensky")

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
API = "https://opensky-network.org/api"

# OpenSky rejects windows longer than 7 days on the airport endpoints.
MAX_WINDOW = 7 * 24 * 3600


class OpenSky:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self.client_id = client_id or os.environ.get("OPENSKY_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("OPENSKY_CLIENT_SECRET")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "gulfwatch/1.0 (personal research)"
        self._token: str | None = None
        self._token_expires = 0.0
        self._auth_failed = False

    @property
    def authenticated(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _auth_header(self) -> dict[str, str]:
        if not self.authenticated:
            return {}
        if self._token and time.time() < self._token_expires - 60:
            return {"Authorization": f"Bearer {self._token}"}
        resp = self.session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + payload.get("expires_in", 1800)
        LOG.info("opensky token refreshed")
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict[str, Any], attempts: int = 4) -> Any:
        # Once OpenSky has refused us there is no point asking 25 more times.
        if self._auth_failed:
            return []
        url = f"{API}{path}"
        for i in range(attempts):
            resp = self.session.get(
                url, params=params, headers=self._auth_header(), timeout=60
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                # OpenSky returns 404 for "no flights in this window"
                return []
            if resp.status_code in (401, 403):
                # Not a transient failure and not something a retry fixes. Do
                # not let it kill the run: the FIR sampler, the EASA scrape and
                # the publish step need no OpenSky credentials and still have
                # useful work to do. Today's coverage score will fall to
                # `outage` on its own, which is the honest outcome.
                self._auth_failed = True
                if self.authenticated:
                    LOG.error(
                        "OpenSky rejected our credentials (%s). Check "
                        "OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET. No flight "
                        "history will be collected this run.", resp.status_code)
                else:
                    LOG.error(
                        "OpenSky refused an anonymous request (%s). Anonymous "
                        "access to the flights endpoints is closed -- set "
                        "OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET. No flight "
                        "history will be collected this run.", resp.status_code)
                return []
            if resp.status_code == 429:
                wait = int(resp.headers.get("X-Rate-Limit-Retry-After-Seconds", 60))
                LOG.warning("rate limited, sleeping %ss", wait)
                time.sleep(min(wait, 300))
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** i)
                continue
            resp.raise_for_status()
        LOG.error("giving up on %s %s", path, params)
        return []

    # --- Airport endpoints -------------------------------------------------

    def departures(self, icao: str, begin: int, end: int) -> list[dict]:
        return self._windowed("/flights/departure", icao, begin, end)

    def arrivals(self, icao: str, begin: int, end: int) -> list[dict]:
        return self._windowed("/flights/arrival", icao, begin, end)

    def _windowed(self, path: str, icao: str, begin: int, end: int) -> list[dict]:
        """Split any request longer than the 7-day cap into legal chunks."""
        out: list[dict] = []
        cursor = int(begin)
        end = int(end)
        while cursor < end:
            stop = min(cursor + MAX_WINDOW, end)
            chunk = self._get(path, {"airport": icao, "begin": cursor, "end": stop})
            if isinstance(chunk, list):
                out.extend(chunk)
            cursor = stop
            time.sleep(1.0)  # be a good citizen; the network is volunteer-run
        return out

    # --- Live state vectors (for FIR transit counting) ---------------------

    def states(self, bbox: tuple[float, float, float, float]) -> list[list]:
        """bbox = (lamin, lomin, lamax, lomax)."""
        lamin, lomin, lamax, lomax = bbox
        data = self._get(
            "/states/all",
            {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax},
        )
        if isinstance(data, dict):
            return data.get("states") or []
        return []
