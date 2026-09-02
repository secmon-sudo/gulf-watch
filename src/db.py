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
    summary      TEXT,             -- plain-language reading of the bulletin
    summary_hash TEXT,             -- body_hash the summary was written from
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

-- Published schedules from AirLabs, cached because the free tier allows 1000
-- requests a month and a timetable changes far more slowly than that.
--
-- This is the answer to the airports OpenSky cannot see. ADS-B says what flew
-- and is blind over Kuwait, Saudi Arabia, Iraq and Iran; a schedule says what
-- the carrier still intends to fly, everywhere, regardless of receivers.
CREATE TABLE IF NOT EXISTS route_schedule (
    dep_iata   TEXT NOT NULL,
    arr_iata   TEXT NOT NULL,
    carrier    TEXT NOT NULL,          -- ICAO operator designator
    weekly     INTEGER,                -- scheduled departures per week
    flights    INTEGER,                -- distinct flight designators
    codeshare  INTEGER DEFAULT 0,
    fetched_at TEXT,
    PRIMARY KEY (dep_iata, arr_iata, carrier)
);

-- Pairs we have asked about, so an empty answer ("nobody flies this") is
-- distinguishable from one we never asked.
CREATE TABLE IF NOT EXISTS schedule_probe (
    dep_iata   TEXT NOT NULL,
    arr_iata   TEXT NOT NULL,
    routes     INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (dep_iata, arr_iata)
);

-- LLM headline classifications, cached by (url, carrier).
--
-- Cached for two reasons. Quota is the lesser one: the same report re-run must
-- give the same answer, and an uncached model call makes the report
-- irreproducible -- which is the one property this project cannot trade away.
CREATE TABLE IF NOT EXISTS headline_class (
    url           TEXT NOT NULL,
    carrier       TEXT NOT NULL,
    action        TEXT,              -- stopped | resumed | unaffected | unclear
                                     --   | irrelevant (not about this region)
    airports      TEXT,              -- comma-separated IATA, validated
    why           TEXT,
    model         TEXT,
    classified_at TEXT,
    PRIMARY KEY (url, carrier)
);

-- Web-search notes, for carriers the other three sources say nothing about.
-- Keyed by day: unlike a headline, a search has no stable identity, so the
-- honest cache key is "what the web said about this carrier today".
CREATE TABLE IF NOT EXISTS carrier_note (
    carrier   TEXT NOT NULL,
    day       TEXT NOT NULL,
    note      TEXT,
    sources   TEXT,              -- newline-separated URLs
    model     TEXT,
    PRIMARY KEY (carrier, day)
);

-- The last set of headlines the press source actually returned for a subject.
-- Google News refuses datacenter IPs often enough that a run can come back
-- with nothing for every query; without this the report has only the outage
-- to show. Cached items are displayed with their age and never feed a
-- carrier's state -- a stale headline setting today's verdict is the exact
-- failure this project keeps having.
CREATE TABLE IF NOT EXISTS headline_cache (
    subject    TEXT NOT NULL,      -- carrier ICAO code, or airport IATA
    kind       TEXT NOT NULL,      -- carrier | airport
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL,      -- JSON list of the kept items
    PRIMARY KEY (subject, kind)
);

-- One row per carrier per report run. The report is a snapshot; what an
-- operator actually wants is the difference between two of them.
CREATE TABLE IF NOT EXISTS report_state (
    day      TEXT NOT NULL,
    carrier  TEXT NOT NULL,
    state    TEXT,
    legs     INTEGER,
    airports TEXT,             -- comma-separated IATA seen at
    PRIMARY KEY (day, carrier)
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

-- Arrival/departure boards for the airports no ADS-B receiver covers. Kept
-- out of `flight` on purpose: those rows are transponder observations with an
-- icao24 address, these are a published board with no aircraft identity, and
-- merging them would let a board entry be counted as a sighting.
-- Raw fetch volume per airport, direction and day: how many records OpenSky
-- returned before any carrier filter. This is the sensor-health signal, and
-- it is deliberately blind to WHO was flying.
--
-- Coverage used to be scored off six named "control" carriers on the reasoning
-- that their disappearance would mean the receivers broke. Measured
-- 2026-09-02, that group was Emirates and Qatar and nobody else: of 12168
-- control legs in August, Lufthansa contributed 1, Air France 4 and KLM none
-- at all. So the health of the whole network rested on two airlines at two
-- airports, and the day one hub's fetch thinned -- Doha ran at 0.27 of
-- baseline on 2026-08-20 -- the score collapsed network-wide. Counting
-- records instead asks about the pipe rather than about anybody's airline.
CREATE TABLE IF NOT EXISTS fetch_probe (
    airport    TEXT NOT NULL,
    direction  TEXT NOT NULL,          -- arr | dep
    day        TEXT NOT NULL,          -- YYYY-MM-DD UTC, from firstSeen
    records    INTEGER NOT NULL,
    fetched_at TEXT,
    PRIMARY KEY (airport, direction, day)
);

CREATE TABLE IF NOT EXISTS board_flight (
    airport    TEXT NOT NULL,          -- ICAO of the monitored airport
    direction  TEXT NOT NULL,          -- arr | dep
    day        TEXT NOT NULL,          -- YYYY-MM-DD UTC
    carrier    TEXT NOT NULL,          -- ICAO of the carrier ON THE TICKET
    flight_no  TEXT NOT NULL,
    other_iata TEXT,                   -- the far end of the leg
    sched_time TEXT,
    fetched_at TEXT,
    -- NULL means `carrier` flies it; anything else is the board's own note
    -- that someone else does, and `carrier` is then only a ticket number.
    -- Read "who operates here" as: carrier WHERE operated_by IS NULL.
    operated_by TEXT,
    PRIMARY KEY (airport, direction, day, carrier, flight_no)
);

-- One row per airport per day, so an empty board can be told from an empty
-- sky. Without this a broken endpoint reads as "every carrier stopped".
CREATE TABLE IF NOT EXISTS board_probe (
    airport    TEXT NOT NULL,
    day        TEXT NOT NULL,
    flights    INTEGER NOT NULL,
    median     REAL,                   -- of prior days, for the comparison
    verdict    TEXT,                   -- ok | thin | empty | unproven
    fetched_at TEXT,
    PRIMARY KEY (airport, day)
);
"""


# Columns added after databases were already in the wild. CREATE TABLE IF NOT
# EXISTS will not add a column to a table that already exists, so a committed
# db would silently keep the old shape and every write would fail.
MIGRATIONS = [
    ("advisory", "summary", "TEXT"),
    ("advisory", "summary_hash", "TEXT"),
    ("board_flight", "operated_by", "TEXT"),
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
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
