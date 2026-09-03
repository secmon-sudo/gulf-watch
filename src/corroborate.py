"""Corroboration: is the silence a decision, or is it something else?

ADS-B silence proves an operator is absent. It does not prove they suspended
anything. Three things look identical from the sensor's point of view:

  * the carrier suspended the service
  * the airport is closed
  * our receivers stopped seeing them

Coverage gating handles the third. This module attacks the first two, using
sources that cost nothing:

  Google News RSS   no key, no quota, returns headlines with dates and links
  FAA NOTAM API     free with registration; tells us if the AIRPORT is shut,
                    which reframes a "carrier stopped" into "nobody can land"

Deliberately keyword-based, not model-based. This layer's job is to hand you
the three links a human should read, and to flag the case where the headlines
say the opposite of the data. It does not decide anything on its own.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from . import config

LOG = logging.getLogger("gulfwatch.corroborate")

NEWS_RSS = "https://news.google.com/rss/search"
FAA_NOTAM = "https://external-api.faa.gov/notamapi/v1/notams"

SUPPORTS = re.compile(
    r"\b(suspend|suspends|suspended|suspension|halt|halts|halted|cancel|"
    r"cancelled|canceled|stops? flights|pauses?|paused|grounded|withdraw)\b", re.I)
CONTRADICTS = re.compile(
    r"\b(resume|resumes|resumed|resumption|restart|restarts|restored|"
    r"returns? to|reinstat)\b", re.I)
CLOSURE = re.compile(r"\b(AD CLSD|AERODROME CLOSED|ARPT CLSD|CLOSED TO ALL)\b", re.I)

_session = requests.Session()
_session.headers["User-Agent"] = "gulfwatch/1.0 (personal research)"


def _news(query: str, limit: int = 6) -> list[dict] | None:
    """Headlines for a query. None means the source did not answer.

    None and [] must stay distinguishable. Google News answers a datacenter IP
    with 503 often enough to matter -- on 2026-08-06 every one of ~35 queries
    from the Actions runner failed, where the same queries returned results
    from a home connection minutes later. Returning [] there made the report
    read "no news about Kuwait" when the truth was "nobody was asked", and it
    dropped the whole press section without saying so.
    """
    url = f"{NEWS_RSS}?{urllib.parse.urlencode({'q': query, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})}"
    try:
        resp = _session.get(url, timeout=25)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as exc:
        LOG.warning("news lookup failed for %r: %s", query, exc)
        return None

    out = []
    for item in list(root.iterfind(".//item"))[:limit]:
        title = (item.findtext("title") or "").strip()
        stance = ("contradicts" if CONTRADICTS.search(title)
                  else "supports" if SUPPORTS.search(title) else "unclear")
        out.append({
            "title": title,
            "url": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "stance": stance,
        })
    return out


def _airport_closed(icao: str) -> bool | None:
    """True/False if the FAA NOTAM API is configured, None if we cannot tell."""
    key = os.environ.get("FAA_CLIENT_ID")
    secret = os.environ.get("FAA_CLIENT_SECRET")
    if not (key and secret):
        return None
    try:
        resp = _session.get(
            FAA_NOTAM,
            params={"icaoLocation": icao, "pageSize": 50},
            headers={"client_id": key, "client_secret": secret},
            timeout=25)
        if resp.status_code != 200:
            return None
        text = resp.text
    except requests.RequestException:
        return None
    return bool(CLOSURE.search(text))


def enrich(conn, max_lookups: int = 12) -> dict:
    """Attach evidence to active suspensions, newest and largest first."""
    carriers = config.carriers()
    airports = config.airports()
    today = datetime.now(tz=timezone.utc).date().isoformat()

    rows = conn.execute(
        """SELECT * FROM suspension WHERE status='active'
           ORDER BY CASE scope WHEN 'region' THEN 0 WHEN 'station' THEN 1 ELSE 2 END,
                    baseline_weekly DESC LIMIT ?""",
        (max_lookups,)).fetchall()

    checked = flagged = closures = 0
    for s in rows:
        name = carriers.get(s["carrier"], {}).get("name", s["carrier"])

        # Airport closure check first -- it reframes the whole event.
        icao = s["detail"] if s["scope"] == "station" else None
        if icao and icao in airports:
            closed = _airport_closed(icao)
            if closed:
                closures += 1
                conn.execute(
                    """INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?)""",
                    (s["id"], "notam",
                     f"{icao} reported closed by NOTAM - suspension may be "
                     f"infrastructure, not a carrier decision",
                     f"https://www.notams.faa.gov/#{icao}", today, "unclear", today))

        where = ""
        if s["scope"] == "station" and icao in airports:
            where = f' "{airports[icao]["city"]}"'
        elif s["scope"] == "route":
            dep, _, arr = s["detail"].partition("-")
            cities = [airports[a]["city"] for a in (dep, arr) if a in airports]
            where = "".join(f' "{c}"' for c in cities)

        query = f'"{name}"{where} (suspend OR suspended OR halt OR resume OR flights)'
        items = _news(query)
        if items is None:
            continue    # the source did not answer; leave the confidence alone
        checked += 1

        # A headline only counts as evidence about this carrier if it names
        # it. Google honours the quotes around the name loosely: the Oman Air
        # query came back with "British Airways to suspend UK repatriation
        # flights" (BBC), which was filed as evidence and pushed an Oman Air
        # route to `corroborated` -- a stop, on the front page, sourced to a
        # story about a different airline. report.carrier_news() has always
        # applied this filter; this path never did.
        #
        # Imported here rather than at module scope: report imports _news from
        # this module, so a top-level import would be circular.
        from .report import _aliases
        keys = _aliases(name)

        stances = set()
        for it in items:
            if not it["url"]:
                continue
            if not any(k in it["title"].lower() for k in keys):
                continue
            stances.add(it["stance"])
            conn.execute(
                "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?)",
                (s["id"], "news", it["title"], it["url"], it["published"],
                 it["stance"], today))

        if "contradicts" in stances:
            confidence = "contradicted"
            flagged += 1
        elif "supports" in stances:
            confidence = "corroborated"
        else:
            confidence = "observed"
        conn.execute("UPDATE suspension SET confidence=? WHERE id=?",
                     (confidence, s["id"]))

    conn.commit()
    LOG.info("corroborated %s events, %s contradicted, %s airport closures",
             checked, flagged, closures)
    return {"checked": checked, "contradicted": flagged, "airport_closures": closures}
