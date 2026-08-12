"""Did they actually stop, when, and are they back?

Frequency ratios answer "how much". They cannot answer "did this carrier stop
serving Dubai on the 14th and resume on the 29th", which is the question that
matters when you are watching a conflict. That needs events with a start, a
duration and an end, carried across runs.

Three scopes, because "stopped" means different things:

  route    QTR|OTHH|OMDB   one city pair dropped
  station  QTR|OMDB        the carrier left an airport entirely
  region   QTR             the carrier vanished from every monitored airport

A station suspension is the headline. A region suspension usually means either
a total network shutdown or -- far more likely -- that something is wrong with
your data, so it is held to a much longer threshold.

Two rules keep this honest:

1. Silent days are COVERAGE-GATED. A day when the sensor network was degraded
   neither extends a silence streak nor breaks it; it is skipped. Otherwise
   every ADS-B outage manufactures a fake suspension with a precise-looking
   start date.
2. A suspension is only ever OPENED against a real baseline. No baseline means
   UNKNOWN, never "stopped".
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from . import config, metrics

LOG = logging.getLogger("gulfwatch.suspensions")

# Consecutive coverage-good silent days before we will call it a stop.
THRESHOLD = {"route": 7, "station": 10, "region": 21}

# Ignore scopes too small to be meaningful (weekly departures in the baseline).
MIN_BASELINE = {"route": 1.0, "station": 2.0, "region": 4.0}

MAX_LOOKBACK = 400


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# --- Scope construction ----------------------------------------------------

def build_scopes(conn) -> dict[str, dict[str, dict]]:
    """Every scope we could suspend, with its baseline and airports."""
    tracked = set(config.tracked_carriers())
    rows = conn.execute(
        "SELECT carrier, dep_icao, arr_icao, weekly_freq FROM baseline"
    ).fetchall()

    scopes: dict[str, dict[str, dict]] = {"route": {}, "station": {}, "region": {}}
    for r in rows:
        c, dep, arr, wf = r["carrier"], r["dep_icao"], r["arr_icao"], r["weekly_freq"]
        if c not in tracked:
            continue
        scopes["route"].setdefault(
            f"{c}|{dep}|{arr}",
            {"carrier": c, "detail": f"{dep}-{arr}", "baseline": 0.0,
             "airports": {dep, arr}},
        )["baseline"] += wf
        for ap in (dep, arr):
            scopes["station"].setdefault(
                f"{c}|{ap}",
                {"carrier": c, "detail": ap, "baseline": 0.0, "airports": {ap}},
            )["baseline"] += wf
        scopes["region"].setdefault(
            c,
            {"carrier": c, "detail": "all monitored airports", "baseline": 0.0,
             "airports": set()},
        )["baseline"] += wf
        scopes["region"][c]["airports"] |= {dep, arr}
    return scopes


def _daily_counts(conn, scope: str, key: str) -> dict[str, int]:
    """Departures per day for a scope."""
    if scope == "route":
        c, dep, arr = key.split("|")
        rows = conn.execute(
            """SELECT day, SUM(departures) n FROM daily_route
               WHERE carrier=? AND dep_icao=? AND arr_icao=? GROUP BY day""",
            (c, dep, arr)).fetchall()
    elif scope == "station":
        c, ap = key.split("|")
        rows = conn.execute(
            """SELECT day, SUM(departures) n FROM daily_route
               WHERE carrier=? AND (dep_icao=? OR arr_icao=?) GROUP BY day""",
            (c, ap, ap)).fetchall()
    else:
        rows = conn.execute(
            "SELECT day, SUM(departures) n FROM daily_route WHERE carrier=? GROUP BY day",
            (key,)).fetchall()
    return {r["day"]: r["n"] for r in rows}


def _coverage_map(conn) -> dict[str, str]:
    return {r["day"]: r["verdict"]
            for r in conn.execute("SELECT day, verdict FROM coverage").fetchall()}


def silence(counts: dict[str, int], cov: dict[str, str], day: date) -> dict:
    """Walk backwards. Returns gated silent days, last operating day, skips.

    Days with bad coverage are skipped entirely -- they do not count as silence
    and they do not reset it. This is the difference between "they stopped on
    the 14th" and "our receivers died on the 14th".

    A day with NO coverage row is skipped for the same reason, and this is not
    a detail. It used to default to "ok", which read a day nobody looked at as
    a day that was looked at and found empty. The baseline harvest ends
    2026-01-31 and observation began 2026-08-01, so every scope had a silent
    stretch of about 190 unlooked-at days behind it. On 2026-08-12, the first
    run whose coverage gate passed, that opened 270 suspensions at once --
    7 whole carriers "stopped" since January, at `confidence: observed`, off a
    hole in the calendar. Absent is not empty.
    """
    silent = skipped = 0
    last_flight = None
    for offset in range(MAX_LOOKBACK):
        d = day - timedelta(days=offset)
        key = d.isoformat()
        if cov.get(key) != "ok":
            skipped += 1
            continue
        if counts.get(key, 0) > 0:
            last_flight = key
            break
        silent += 1
    return {"silent_days": silent, "last_flight_on": last_flight,
            "coverage_days_skipped": skipped}


def first_flight_after(counts: dict[str, int], start: str) -> str | None:
    later = [d for d, n in counts.items() if n > 0 and d >= start]
    return min(later) if later else None


# --- The state machine -----------------------------------------------------

def detect(conn, day: date | None = None) -> dict:
    day = day or metrics.reference_day()
    cov = _coverage_map(conn)
    if cov.get(day.isoformat(), "ok") != "ok":
        LOG.warning("coverage not ok for %s; suspension state left untouched", day)
        return {"opened": 0, "resumed": 0, "skipped": True,
                "opened_events": [], "resumed_events": []}

    scopes = build_scopes(conn)
    opened: list[dict] = []
    resumed: list[dict] = []

    for scope, entries in scopes.items():
        limit = THRESHOLD[scope]
        floor = MIN_BASELINE[scope]
        for key, meta in entries.items():
            if meta["baseline"] < floor:
                continue
            counts = _daily_counts(conn, scope, key)
            s = silence(counts, cov, day)

            active = conn.execute(
                "SELECT * FROM suspension WHERE scope=? AND scope_key=? AND status='active'",
                (scope, key)).fetchone()

            # --- resumption ------------------------------------------------
            if s["silent_days"] == 0 and active:
                back = first_flight_after(counts, active["started_on"]) or day.isoformat()
                # A resumption cannot predate the stop it ends. That only
                # happens when the event and the traffic behind it have drifted
                # apart (a rebuilt daily_route, a restored db). Clamp rather
                # than publish a negative duration.
                back = max(back, active["started_on"])
                stopped_for = (_d(back) - _d(active["started_on"])).days
                conn.execute(
                    """UPDATE suspension SET status='resumed', resumed_on=?,
                       days_stopped=? WHERE id=?""",
                    (back, stopped_for, active["id"]))
                resumed.append({"scope": scope, "carrier": meta["carrier"],
                                "detail": meta["detail"], "resumed_on": back,
                                "days_stopped": stopped_for})
                LOG.info("RESUMED %s %s after %s days", scope, key, stopped_for)
                continue

            # --- still stopped: keep the duration current -------------------
            if active:
                conn.execute(
                    "UPDATE suspension SET days_stopped=? WHERE id=?",
                    ((day - _d(active["started_on"])).days, active["id"]))
                continue

            # --- new stop ---------------------------------------------------
            if s["silent_days"] >= limit:
                if s["last_flight_on"]:
                    started = (_d(s["last_flight_on"]) + timedelta(days=1)).isoformat()
                else:
                    # never seen operating in our window; do not invent a date
                    started = (day - timedelta(days=s["silent_days"])).isoformat()
                conn.execute(
                    """INSERT OR IGNORE INTO suspension
                       (scope, scope_key, carrier, detail, baseline_weekly,
                        last_flight_on, started_on, detected_on, days_stopped,
                        status, confidence)
                       VALUES (?,?,?,?,?,?,?,?,?, 'active', 'observed')""",
                    (scope, key, meta["carrier"], meta["detail"],
                     round(meta["baseline"], 1), s["last_flight_on"], started,
                     day.isoformat(), (day - _d(started)).days))
                opened.append({"scope": scope, "carrier": meta["carrier"],
                               "detail": meta["detail"], "started_on": started,
                               "days_stopped": (day - _d(started)).days,
                               "baseline_weekly": round(meta["baseline"], 1)})
                LOG.info("STOPPED %s %s since %s", scope, key, started)

    conn.commit()
    return {"opened": len(opened), "resumed": len(resumed), "skipped": False,
            "opened_events": opened, "resumed_events": resumed}


# --- Reporting -------------------------------------------------------------

def report(conn, resumed_window_days: int = 30) -> dict:
    carriers = config.carriers()
    active = [dict(r) for r in conn.execute(
        """SELECT * FROM suspension WHERE status='active'
           ORDER BY CASE scope WHEN 'region' THEN 0 WHEN 'station' THEN 1 ELSE 2 END,
                    baseline_weekly DESC""")]
    cutoff = (metrics.today_utc() - timedelta(days=resumed_window_days)).isoformat()
    recent = [dict(r) for r in conn.execute(
        "SELECT * FROM suspension WHERE status='resumed' AND resumed_on >= ? "
        "ORDER BY resumed_on DESC", (cutoff,))]

    regions = {s["carrier"] for s in active if s["scope"] == "region"}
    stations = {(s["carrier"], s["detail"]) for s in active if s["scope"] == "station"}

    for s in active + recent:
        s["carrier_name"] = carriers.get(s["carrier"], {}).get("name", s["carrier"])
        s["evidence"] = [dict(e) for e in conn.execute(
            "SELECT source, title, url, published, stance FROM evidence "
            "WHERE suspension_id=? ORDER BY published DESC LIMIT 5", (s["id"],))]
        # Roll the hierarchy up. If a carrier has left the region entirely,
        # listing each airport and route it also left is noise.
        if s["scope"] == "region":
            s["superseded_by"] = None
        elif s["scope"] == "station":
            s["superseded_by"] = "region" if s["carrier"] in regions else None
        else:
            dep, _, arr = s["detail"].partition("-")
            s["superseded_by"] = (
                "region" if s["carrier"] in regions
                else "station" if ((s["carrier"], dep) in stations
                                   or (s["carrier"], arr) in stations)
                else None)

    return {
        "active": active,
        "recently_resumed": recent,
        "summary": {
            "carriers_with_any_stop": len({s["carrier"] for s in active}),
            "region_stops": sum(1 for s in active if s["scope"] == "region"),
            "station_stops": sum(1 for s in active if s["scope"] == "station"),
            "route_stops": sum(1 for s in active if s["scope"] == "route"),
            "resumed_last_30d": len(recent),
        },
    }
