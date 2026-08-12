"""Configuration loading and tunable thresholds for GulfWatch."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("GULFWATCH_DATA_DIR", ROOT / "data"))
PUBLIC_DIR = Path(os.environ.get("GULFWATCH_PUBLIC_DIR", ROOT / "public"))
DB_PATH = DATA_DIR / "gulfwatch.db"


# --- Thresholds ------------------------------------------------------------
# Frequency is always measured as departures per rolling 7 days, per
# (carrier, origin, destination). Ratios below are current / baseline.

STATUS_NORMAL_MIN = 0.80      # >= 80% of baseline
STATUS_REDUCED_MIN = 0.30     # 30-80%
STATUS_MINIMAL_MIN = 0.01     # 1-30%; below that -> SUSPENDED

# A route must be silent for this many consecutive days before we will call it
# SUSPENDED. Prevents a single bad ingest from firing an alert.
SUSPENSION_CONFIRM_DAYS = 3

# Coverage health: fraction of the trailing 28-day median volume that the
# control-group carriers must still show for us to trust today's data.
COVERAGE_OK = 0.70
COVERAGE_DEGRADED = 0.40      # below this -> all alerts suppressed

# How far back each run re-judges coverage. A day's flights can arrive after
# the day was first scored -- ingest reads 48h, the backfill writes months --
# and a verdict that is never revisited goes stale. 14 covers the lookback with
# room to spare; older days no longer change.
COVERAGE_RESCORE_DAYS = 14

# How many of the 7 rolling days must have passed the coverage gate before a
# current-vs-baseline ratio is published at all. Below this the traffic we hold
# is scaled up from too little: on 2026-08-12 the window held 2 observed days,
# and dividing two days of flying by a seven-day baseline read Qatar Airways at
# 23% of normal when it was running at about 80%. Matches the floor
# report.observed_vs_scheduled has always applied to its own ratio.
MIN_OBSERVED_DAYS = 5

# The same test applied to the reference period. A route's baseline is only
# usable if one of its monitored endpoints was actually being seen across the
# window it was harvested from -- as a fraction of that window's length.
# Measured over the frozen 2025-11-01..2026-01-31 window, the monitored
# airports split cleanly either side of this: Doha, Dubai, Bahrain and Sharjah
# on 88 of 92 days and Abu Dhabi on 87, then Kuwait on 37, Amman on 24, Beirut
# on 17 and the blind seven on 0. Dividing today's traffic by a baseline from
# an airport nobody was watching reads as growth, not as the gap it is.
MIN_BASELINE_COVERAGE = 0.75

# Day counts miss the airport whose fetch ran and came back nearly empty. The
# control carriers settle it: their rate at an airport now, over their rate
# there in the baseline window. Stable by construction, so a large jump is a
# statement about our receivers. Measured 2026-08-12: Dubai 0.7x, Doha 0.8x,
# Sharjah 0.7x; Abu Dhabi 18x, Amman 49x, Beirut 81x. Nothing sits near 3.
MAX_BASELINE_CONTROL_DRIFT = 3.0

# Baseline window (frozen, pre-escalation). Change and re-run backfill if you
# want a different reference period.
BASELINE_START = os.environ.get("GULFWATCH_BASELINE_START", "2025-11-01")
BASELINE_END = os.environ.get("GULFWATCH_BASELINE_END", "2026-01-31")

# Ingest lookback. Every run re-reads this many hours and upserts, so late
# arriving OpenSky data is picked up instead of lost.
INGEST_LOOKBACK_HOURS = 48

# How far back the last *settled* UTC day is. Everything analytical -- coverage
# scoring, route status, stop/resume detection -- is anchored here rather than
# on "now".
#
# The current day is always structurally incomplete: at 10:00 UTC only 40% of
# it has happened, and OpenSky publishes with a further lag on top of that.
# Scoring it against a median of complete days makes coverage read `outage` on
# essentially every run, which suppresses every alert and freezes the stop
# detector -- a monitor that has collected the data and refuses to say
# anything. Raise this if your OpenSky data settles more slowly.
SETTLE_LAG_DAYS = 1

# ADS-B position quality floor for FIR overflight counting. The Gulf has heavy
# GNSS jamming/spoofing; low-integrity positions must not be counted.
MIN_NIC = 7
MIN_SIL = 2


def _load(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def carriers() -> dict[str, dict]:
    return _load("carriers.yml")["carriers"]


def airports() -> dict[str, dict]:
    return _load("airports.yml")["airports"]


def firs() -> dict[str, dict]:
    return _load("airports.yml")["firs"]


def tracked_carriers() -> dict[str, dict]:
    return {k: v for k, v in carriers().items() if v.get("tracked")}


def control_carriers() -> dict[str, dict]:
    return {k: v for k, v in carriers().items() if v.get("control")}
