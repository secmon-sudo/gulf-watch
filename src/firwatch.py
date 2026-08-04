"""Who is still crossing the conflict-zone FIRs.

This is the metric that actually answers the question. Carriers rarely announce
a suspension; they quietly reroute. Counting distinct airframes inside Tehran,
Baghdad, Beirut and Damascus FIRs shows compliance with the EASA CZIB advisories
far earlier than a frequency drop at DOH or DXB does.

Each run is a snapshot, not a census. Run it often; trend the daily distinct
aircraft count rather than reading any single sample as truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import adsb_live, config
from .parse import parse_callsign

LOG = logging.getLogger("gulfwatch.fir")


def _centre(bbox) -> tuple[float, float, int]:
    lamin, lomin, lamax, lomax = bbox
    lat = (lamin + lamax) / 2
    lon = (lomin + lomax) / 2
    # crude radius: half the diagonal in nm, capped at the API limit
    span_nm = max(lamax - lamin, (lomax - lomin) * 0.85) * 60 / 2
    return lat, lon, int(min(max(span_nm, 50), 250))


def _inside(ac: dict, bbox) -> bool:
    lamin, lomin, lamax, lomax = bbox
    return lamin <= ac["lat"] <= lamax and lomin <= ac["lon"] <= lomax


def sample(conn, only_czib: bool = False) -> dict:
    """One sampling pass over configured FIRs. Writes to fir_transit."""
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    carriers = config.carriers()
    results: dict[str, dict[str, int]] = {}
    rejected = 0

    for fir, cfg in config.firs().items():
        if only_czib and not cfg.get("czib_watch"):
            continue
        lat, lon, radius = _centre(cfg["bbox"])
        backend, aircraft = adsb_live.point(lat, lon, radius)
        seen: dict[str, set] = {}
        for ac in aircraft:
            if not adsb_live.position_is_trustworthy(ac):
                rejected += 1
                continue
            if not _inside(ac, cfg["bbox"]):
                continue
            carrier, _ = parse_callsign(ac.get("flight"))
            if not carrier or carrier not in carriers:
                continue
            seen.setdefault(carrier, set()).add(ac.get("hex"))

        for carrier, hexes in seen.items():
            conn.execute(
                """INSERT INTO fir_transit (day, fir, carrier, aircraft)
                   VALUES (?,?,?,?)
                   ON CONFLICT(day, fir, carrier) DO UPDATE SET
                     aircraft = MAX(fir_transit.aircraft, excluded.aircraft)""",
                (day, fir, carrier, len(hexes)),
            )
        results[fir] = {c: len(h) for c, h in seen.items()}
        LOG.info("%s via %s: %s carriers", fir, backend, len(seen))

    conn.commit()
    LOG.info("dropped %s positions below the GNSS integrity floor", rejected)
    return {"day": day, "firs": results, "positions_rejected": rejected}


def summary(conn, days: int = 7) -> list[dict]:
    rows = conn.execute(
        """SELECT fir, carrier, SUM(aircraft) AS n
           FROM fir_transit
           WHERE day >= date('now', ?)
           GROUP BY fir, carrier ORDER BY fir, n DESC""",
        (f"-{days} day",),
    ).fetchall()
    firs = config.firs()
    # Pre-seed every FIR at zero. A conflict-zone FIR with no traffic is the
    # headline finding, so it must appear in the output rather than vanish
    # because there was no row to group.
    out: dict[str, dict] = {
        code: {"fir": code, "name": cfg.get("name", code),
               "czib_watch": cfg.get("czib_watch", False),
               "carriers": [], "total_transits": 0}
        for code, cfg in firs.items()
    }
    for r in rows:
        entry = out.setdefault(r["fir"], {
            "fir": r["fir"],
            "name": firs.get(r["fir"], {}).get("name", r["fir"]),
            "czib_watch": firs.get(r["fir"], {}).get("czib_watch", False),
            "carriers": [],
            "total_transits": 0,
        })
        entry["carriers"].append({"carrier": r["carrier"], "transits": r["n"]})
        entry["total_transits"] += r["n"]
    return list(out.values())
