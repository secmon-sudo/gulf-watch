"""SQLite storage. One file, committed to the repo, git gives us history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;

-- One row per observed flight leg. icao24 + first_seen is the natural key,
-- so re-ingesting the same window is idempotent.
CREATE TABLE IF NOT EXISTS flight (
    icao24        TEXT NOT NULL,
    first_seen    INTEGER NOT NULL,       -- unix seconds
    last_seen     INTEGER,
    callsign      TEXT,
    carrier       TEXT,                   -- ICAO prefix, e.g. QTR
    flight_number INTEGER,
    dep_icao      TEXT,
    arr_icao      TEXT,
    is_freight    INTEGER DEFAULT 0,
    dep_date      TEXT,                   -- YYYY-MM-DD (UTC) of first_seen
    source        TEXT,                   -- opensky | adsblol | airplaneslive
    ingested_at   INTEGER,
    PRIMARY KEY (icao24, first_seen)
);
CREATE INDEX IF NOT EXISTS ix_flight_route ON flight (carrier, dep_icao, arr_icao, dep_date);
CREATE INDEX IF NOT EXISTS ix_flight_date  ON flight (dep_date);

-- Frozen pre-escalation reference. Written once by backfill.py.
CREATE TABLE IF NOT EXISTS baseline (
    carrier       TEXT NOT NULL,
    dep_icao      TEXT NOT NULL,
    arr_icao      TEXT NOT NULL,
    weekly_freq   REAL NOT NULL,          -- mean departures per 7 days
    sample_days   INTEGER NOT NULL,
    window_start  TEXT,
    window_end    TEXT,
    PRIMARY KEY (carrier, dep_icao, arr_icao)
);

-- Daily rollup, recomputed on every run.
CREATE TABLE IF NOT EXISTS daily_route (
    day           TEXT NOT NULL,
    carrier       TEXT NOT NULL,
    dep_icao      TEXT NOT NULL,
    arr_icao      TEXT NOT NULL,
    departures    INTEGER NOT NULL,
    PRIMARY KEY (day, carrier, dep_icao, arr_icao)
);

-- Coverage integrity per day. If this is bad, nothing else is trustworthy.
CREATE TABLE IF NOT EXISTS coverage (
    day             TEXT PRIMARY KEY,
    control_flights INTEGER,
    median_28d      REAL,
    score           REAL,
    verdict         TEXT                  -- ok | degraded | outage
);

-- FIR overflight counts from live ADS-B sampling.
CREATE TABLE IF NOT EXISTS fir_transit (
    day       TEXT NOT NULL,
    fir       TEXT NOT NULL,
    carrier   TEXT NOT NULL,
    aircraft  INTEGER NOT NULL,           -- distinct icao24 seen inside the FIR
    PRIMARY KEY (day, fir, carrier)
);

-- EASA CZIB / NOTAM advisory state, hashed for change detection.
CREATE TABLE IF NOT EXISTS advisory (
    source     TEXT NOT NULL,             -- easa_czib | faa_notam
    ref        TEXT NOT NULL,             -- e.g. CZIB-2026-05
    revision   TEXT,
    title      TEXT,
    valid_to   TEXT,
    body_hash  TEXT,
    url        TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    PRIMARY KEY (source, ref)
);

-- Suspension EVENTS, not statuses. A suspension has a start, a duration and
-- (hopefully) an end. Point-in-time status cannot answer "when did they stop"
-- or "are they back yet", which is the question that actually matters.
CREATE TABLE IF NOT EXISTS suspension (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL,      -- route | station | region
    scope_key       TEXT NOT NULL,      -- QTR|OTHH|OMDB  /  QTR|OMDB  /  QTR
    carrier         TEXT NOT NULL,
    detail          TEXT,               -- human label: "OTHH-OMDB", "OMDB", "all monitored airports"
    baseline_weekly REAL,
    last_flight_on  TEXT,               -- last day we saw them operate
    started_on      TEXT NOT NULL,      -- first silent day
    detected_on     TEXT,               -- day the event crossed the threshold
    resumed_on      TEXT,               -- NULL while still stopped
    days_stopped    INTEGER,
    status          TEXT NOT NULL,      -- active | resumed
    confidence      TEXT,               -- observed | corroborated | contradicted
    UNIQUE (scope, scope_key, started_on)
);
CREATE INDEX IF NOT EXISTS ix_susp_active ON suspension (status, scope);

-- Corroboration for a suspension. ADS-B silence says an aircraft is absent.
-- Only an announcement or a NOTAM says an operator stopped on purpose.
CREATE TABLE IF NOT EXISTS evidence (
    suspension_id INTEGER NOT NULL,
    source        TEXT NOT NULL,        -- news | notam | czib
    title         TEXT,
    url           TEXT NOT NULL,
    published     TEXT,
    stance        TEXT,                 -- supports | contradicts | unclear
    found_on      TEXT,
    PRIMARY KEY (suspension_id, url)
);

-- Which backfill slices have actually landed. OpenSky's daily allowance is far
-- smaller than a full baseline harvest, so the harvest must survive being cut
-- off and resumed tomorrow -- and freeze() must be able to tell whether it is
-- looking at a complete window or a fragment.
CREATE TABLE IF NOT EXISTS backfill_progress (
    airport      TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    legs         INTEGER,
    done_at      TEXT,
    PRIMARY KEY (airport, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS run_log (
    started_at TEXT PRIMARY KEY,
    kind       TEXT,
    ok         INTEGER,
    detail     TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_flights(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert-or-replace flight legs. Returns rows written."""
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO flight (icao24, first_seen, last_seen, callsign, carrier,
                            flight_number, dep_icao, arr_icao, is_freight,
                            dep_date, source, ingested_at)
        VALUES (:icao24, :first_seen, :last_seen, :callsign, :carrier,
                :flight_number, :dep_icao, :arr_icao, :is_freight,
                :dep_date, :source, :ingested_at)
        ON CONFLICT(icao24, first_seen) DO UPDATE SET
            last_seen = excluded.last_seen,
            arr_icao  = COALESCE(excluded.arr_icao, flight.arr_icao),
            dep_icao  = COALESCE(excluded.dep_icao, flight.dep_icao),
            callsign  = COALESCE(excluded.callsign, flight.callsign)
        """,
        rows,
    )
    conn.commit()
    return len(rows)
