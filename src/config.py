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

# Baseline window (frozen, pre-escalation). Change and re-run backfill if you
# want a different reference period.
BASELINE_START = os.environ.get("GULFWATCH_BASELINE_START", "2025-11-01")
BASELINE_END = os.environ.get("GULFWATCH_BASELINE_END", "2026-01-31")

# Ingest lookback. Every run re-reads this many hours and upserts, so late
# arriving OpenSky data is picked up instead of lost.
INGEST_LOOKBACK_HOURS = 48

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
