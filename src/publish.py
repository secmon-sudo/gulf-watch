"""Generate the published API as static JSON files.

No server, no database service, no cold starts, nothing to pay for. GitHub
Pages serves the folder and you get a real, cacheable, CORS-open API:

    /v1/status.json
    /v1/suspensions.json
    /v1/alerts.json
    /v1/airlines/index.json
    /v1/airlines/QTR.json
    /v1/airports/index.json
    /v1/airports/OTHH.json
    /v1/firs.json
    /v1/advisories.json
    /v1/health.json

Every payload carries `generated_at`, `coverage`, and `attribution`. A consumer
that cannot see the coverage verdict cannot judge the numbers, and the ODbL
licence on the ADS-B data requires the attribution.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import advisories, config, db, firwatch, metrics, suspensions

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("gulfwatch.publish")

ATTRIBUTION = {
    "flight_data": "OpenSky Network (opensky-network.org), non-commercial use",
    "live_adsb": "adsb.lol and airplanes.live, ODbL 1.0",
    "advisories": "EASA Conflict Zone Information Bulletins",
    "notice": "Volunteer ADS-B coverage. Absence of data is not evidence of "
              "absence of flights. Check the coverage block before use.",
}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    LOG.info("wrote %s", path.relative_to(config.PUBLIC_DIR.parent))


def _envelope(report: dict) -> dict:
    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "as_of_day": report["day"],
        "coverage": report["coverage"],
        # How much of the rolling window we actually saw. Every ratio in the
        # payload is scaled by this, and withheld entirely when it is short --
        # a reader who cannot see it cannot tell a cut from our own downtime.
        "observation": {
            "window_days": report["window_days"],
            "observed_days": report["observed_days"],
            "min_observed_days": report["min_observed_days"],
            "ratios_published": report["ratios_published"],
        },
        "attribution": ATTRIBUTION,
    }


def build(out: Path | None = None) -> dict:
    out = Path(out or config.PUBLIC_DIR) / "v1"
    conn = db.connect()
    report = metrics.route_report(conn)
    stops = suspensions.report(conn)
    env = _envelope(report)
    active_by_carrier: dict[str, list] = {}
    for ev in stops["active"]:
        active_by_carrier.setdefault(ev["carrier"], []).append(ev)
    routes = report["routes"]
    carriers = config.carriers()
    airports = config.airports()

    # --- /v1/status.json : carrier-level roll-up -------------------------
    by_carrier: dict[str, dict] = {}
    for r in routes:
        c = by_carrier.setdefault(r["carrier"], {
            "carrier": r["carrier"],
            "name": r["carrier_name"],
            "iata": carriers.get(r["carrier"], {}).get("iata"),
            "country": carriers.get(r["carrier"], {}).get("country"),
            "weekly_frequency": 0,
            "weekly_scaled": 0.0,
            "baseline_weekly": 0.0,
            "routes_total": 0,
            "routes_suspended": 0,
            "routes_reduced": 0,
        })
        c["weekly_frequency"] += r["weekly_frequency"]
        c["weekly_scaled"] += r["weekly_scaled"] or 0.0
        c["baseline_weekly"] += r["baseline_weekly"]
        c["routes_total"] += 1
        if r["status"] == "SUSPENDED":
            c["routes_suspended"] += 1
        elif r["status"] in ("REDUCED", "MINIMAL"):
            c["routes_reduced"] += 1

    for code, c in by_carrier.items():
        c["baseline_weekly"] = round(c["baseline_weekly"], 1)
        c["observed_days"] = report["observed_days"]
        if report["ratios_published"]:
            # Compare like with like: the scaled week against a weekly baseline.
            c["weekly_scaled"] = round(c["weekly_scaled"], 1)
            status, ratio = metrics.classify(c["weekly_scaled"], c["baseline_weekly"])
        else:
            c["weekly_scaled"] = None
            status, ratio = "UNKNOWN", None
        c["ratio"] = ratio
        c["status"] = status if report["coverage"]["verdict"] == "ok" else "UNKNOWN"

        # The direct answer: has this carrier stopped anything, and since when?
        evs = active_by_carrier.get(code, [])
        region = [e for e in evs if e["scope"] == "region"]
        station = [e for e in evs if e["scope"] == "station"]
        c["operating"] = not region
        c["stopped"] = {
            "any": bool(evs),
            "scope": ("region" if region else "station" if station
                      else "route" if evs else None),
            "airports_dropped": sorted({e["detail"] for e in station}),
            "routes_dropped": sorted({e["detail"] for e in evs
                                      if e["scope"] == "route"}),
            "since": min((e["started_on"] for e in evs), default=None),
            "days_stopped": max((e["days_stopped"] or 0 for e in evs), default=0),
            "confidence": (region + station + evs)[0]["confidence"] if evs else None,
        }

    _write(out / "status.json", {
        **env,
        "carriers": sorted(by_carrier.values(), key=lambda x: x["name"]),
        "summary": {
            "carriers_tracked": len(by_carrier),
            "routes_tracked": len(routes),
            "routes_suspended": sum(1 for r in routes if r["status"] == "SUSPENDED"),
            "routes_reduced": sum(1 for r in routes
                                  if r["status"] in ("REDUCED", "MINIMAL")),
            "carriers_with_a_stop": stops["summary"]["carriers_with_any_stop"],
            "stations_dropped": stops["summary"]["station_stops"],
            "resumed_last_30d": stops["summary"]["resumed_last_30d"],
        },
    })

    # --- /v1/airlines/{ICAO}.json ----------------------------------------
    index = []
    for code, c in by_carrier.items():
        legs = [r for r in routes if r["carrier"] == code]
        _write(out / "airlines" / f"{code}.json", {
            **env, **c, "routes": legs,
            "stop_events": active_by_carrier.get(code, []),
            "resumed": [e for e in stops["recently_resumed"] if e["carrier"] == code],
        })
        index.append({"carrier": code, "name": c["name"], "status": c["status"],
                      "operating": c["operating"], "stopped": c["stopped"]["any"],
                      "href": f"airlines/{code}.json"})
    _write(out / "airlines" / "index.json", {**env, "airlines": sorted(
        index, key=lambda x: x["name"])})

    # --- /v1/airports/{ICAO}.json ----------------------------------------
    ap_index = []
    for icao, meta in airports.items():
        legs = [r for r in routes if r["origin"] == icao or r["destination"] == icao]
        if not legs:
            continue
        payload = {
            **env,
            "airport": icao,
            "iata": meta["iata"],
            "name": meta["name"],
            "city": meta["city"],
            "country": meta["country"],
            "weekly_frequency": sum(r["weekly_frequency"] for r in legs),
            "baseline_weekly": round(sum(r["baseline_weekly"] for r in legs), 1),
            "carriers_operating": sorted({r["carrier"] for r in legs
                                          if r["weekly_frequency"] > 0}),
            "carriers_absent": sorted({r["carrier"] for r in legs
                                       if r["status"] == "SUSPENDED"}),
            "carriers_stopped": [
                {"carrier": e["carrier"], "name": e["carrier_name"],
                 "since": e["started_on"], "days_stopped": e["days_stopped"],
                 "confidence": e["confidence"]}
                for e in stops["active"]
                if e["scope"] == "station" and e["detail"] == icao],
            "resumed_recently": [
                {"carrier": e["carrier"], "name": e["carrier_name"],
                 "resumed_on": e["resumed_on"], "was_stopped_days": e["days_stopped"]}
                for e in stops["recently_resumed"]
                if e["scope"] == "station" and e["detail"] == icao],
            "routes": legs,
        }
        _write(out / "airports" / f"{icao}.json", payload)
        ap_index.append({"airport": icao, "iata": meta["iata"], "name": meta["name"],
                         "href": f"airports/{icao}.json"})
    _write(out / "airports" / "index.json", {**env, "airports": ap_index})

    # --- /v1/suspensions.json --------------------------------------------
    _write(out / "suspensions.json", {
        **env,
        "thresholds_days": suspensions.THRESHOLD,
        "reading_note": (
            "started_on is the first silent day, not an announcement date. "
            "confidence=observed means ADS-B silence only; corroborated means "
            "reporting agrees; contradicted means reporting says they are "
            "flying and you should trust the reporting over this feed."),
        "summary": stops["summary"],
        "active": stops["active"],
        "recently_resumed": stops["recently_resumed"],
    })

    # --- /v1/alerts.json --------------------------------------------------
    alerts = metrics.alerts_from(report)
    _write(out / "alerts.json", {
        **env,
        "suppressed": report["coverage"]["verdict"] != "ok",
        "suppression_reason": (
            None if report["coverage"]["verdict"] == "ok"
            else "ADS-B coverage below threshold; absence of flights cannot be "
                 "distinguished from absence of data"
        ),
        "alerts": alerts,
    })

    # --- /v1/firs.json ----------------------------------------------------
    _write(out / "firs.json", {**env, "window_days": 7,
                               "firs": firwatch.summary(conn, days=7)})

    # --- /v1/advisories.json ----------------------------------------------
    _write(out / "advisories.json", {**env, "czib": advisories.current(conn)})

    # --- /v1/health.json --------------------------------------------------
    last = conn.execute(
        "SELECT started_at, detail FROM run_log ORDER BY started_at DESC LIMIT 5"
    ).fetchall()
    # Two numbers, because after backfill.compact() there are genuinely two.
    # The rollup is the durable record and what every analysis reads; the raw
    # legs are whatever has not been folded away yet. A single "flight_legs"
    # counted one table and would have meant different things on either side
    # of a compaction, which is the kind of quietly shifting figure this
    # report exists to avoid. The span covers both, so it stays right in both
    # states.
    roll = conn.execute(
        "SELECT MIN(day) a, MAX(day) b, SUM(departures) n FROM daily_route"
    ).fetchone()
    raw = conn.execute(
        "SELECT MIN(dep_date) a, MAX(dep_date) b, COUNT(*) n FROM flight"
    ).fetchone()
    days = [d for d in (roll["a"], roll["b"], raw["a"], raw["b"]) if d]
    baseline_rows = conn.execute("SELECT COUNT(*) n FROM baseline").fetchone()["n"]
    _write(out / "health.json", {
        **env,
        "database": {"route_departures": roll["n"] or 0,
                     "raw_legs_retained": raw["n"],
                     "first_day": min(days) if days else None,
                     "last_day": max(days) if days else None,
                     "baseline_routes": baseline_rows},
        "baseline_ready": baseline_rows > 0,
        "recent_runs": [dict(r) for r in last],
    })

    _write(out / "index.json", {
        **env,
        "endpoints": ["status.json", "suspensions.json", "alerts.json", "firs.json",
                      "advisories.json", "health.json",
                      "airlines/index.json", "airports/index.json"],
    })
    return report


if __name__ == "__main__":
    build()
