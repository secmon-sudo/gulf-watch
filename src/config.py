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

# The same question asked of the OBSERVATION window, per airport and per fetch
# direction. The day-level verdict is global -- it scores the whole network
# against its own 28-day median -- so a day passes as `ok` while one hub's
# fetch returns a third of its usual volume, and silence at that hub reads as
# an airline decision. Measured over 2026-08-15..19, every day of it `ok`:
# Doha's *departure* fetch ran at 0.27-0.56 of baseline while its *arrival*
# fetch held at 0.66 and kept showing the same routes flying the other way.
# That gap opened nine route stops on 2026-08-20, four of them out of Doha,
# every one of them contradicted by its own return leg. A leg is only ever
# seen through one of the two fetches, so the direction is half the question.
# At this floor a daily route is missed on all seven silent days with
# probability about 1 in 128; at the 0.27 Doha was actually delivering it is
# worse than 1 in 10, which is the difference between a finding and a guess.
MIN_CURRENT_AIRPORT_COVERAGE = 0.5

# Day counts miss the airport whose fetch ran and came back nearly empty. The
# control carriers settle it: their rate at an airport now, over their rate
# there in the baseline window. Stable by construction, so a large jump is a
# statement about our receivers. Measured 2026-08-12: Dubai 0.7x, Doha 0.8x,
# Sharjah 0.7x; Abu Dhabi 18x, Amman 49x, Beirut 81x. Nothing sits near 3.
MAX_BASELINE_CONTROL_DRIFT = 3.0

# Coverage is not the only way a baseline fails to support a ratio. A route
# expected to fly less than once a week cannot be judged over a seven-day
# window: the expected count is under one departure, so seeing none or two
# swings the percentage across the whole scale. Measured 2026-08-12, this
# excludes 569 of 1163 trusted routes -- half of them -- carrying 2.9% of the
# weekly departures. Nearly free in coverage, and it is where the noise lives.
MIN_BASELINE_WEEKLY = 1.0

# And a carrier-level ratio must be about the carrier, not about a sliver of
# it. Etihad had 12 comparable routes of 100 and would have published the
# Doha-Abu Dhabi shuttle as the state of Etihad. Measured as the comparable
# share of everything known about the carrier -- traffic seen plus traffic
# expected, so a carrier that is genuinely flying nothing still qualifies on
# its baseline alone and can still read SUSPENDED. Measured 2026-08-12:
# Emirates 97%, Qatar 96%, British Airways 96%, Kuwait 84%, Pegasus 73%,
# against Kuwait's neighbours Oman 36%, EgyptAir 19%, Etihad 5%, Royal
# Jordanian 1%. Nothing sits between 42% and 73%.
MIN_COMPARABLE_SHARE = 0.6

# The same question asked of a CARRIER rather than an airport or a fetch, and
# the only one of the five gates that can tell a British Airways from an
# Emirates. Every earlier gate is geographic -- did this airport's fetch run,
# can this endpoint be named -- and geography cannot separate them, because the
# two fly the same pair on the same day through the same receivers. Measured
# 2026-08-24 by asking OpenSky for 2026-08-19 by origin airport instead of by
# callsign: seventy-one arrivals into Dubai came from European airports and
# every one was Emirates or flydubai, four of them EGLL-OMDB. BA runs that pair
# daily and appears under no callsign at all. Whatever the cause, the feed
# carries some operators and not others, and no gate we write and no window we
# widen changes that.
#
# So: legs seen over the observed days, against what this carrier's own
# baseline predicts for the same days. The split is not close. Over the
# thirteen observed days to 2026-08-23: Qatar 0.68, Emirates 0.74, Oman Air
# 0.85, flydubai 0.87, flynas 0.91, Air Arabia 0.96 and up -- then nothing at
# all until Kuwait Airways 0.08, British Airways 0.008, and JAL, Finnair,
# American, Iberia, China Southern, Air China, China Eastern and Air Algerie at
# exactly zero. Nothing lands between 0.08 and 0.68.
#
# A carrier below this floor is not scored and cannot be called stopped. That
# withholds a genuine total suspension too, and knowingly: this feed cannot
# tell one from the other, and the project's answer to that has always been to
# withhold rather than to guess. The press route still works -- report.verdict
# calls a carrier stopped on schedule plus reporting, and needs no sighting.
MIN_CARRIER_VISIBILITY = 0.2

# Over how many calendar days the question is asked. Deliberately far wider
# than the seven-day ratio window: whether we can see a carrier at all is a
# property of the feed, not of the week, and a small carrier can miss a week by
# chance. 42 days matches the region-scope span and covers the whole
# observation record as it stands.
CARRIER_VISIBILITY_DAYS = 42

# Days of raw-fetch history needed before coverage is scored on signal volume
# rather than on the old control-carrier count. Seven: enough for a median to
# mean something, short enough that the fallback is not load-bearing for long.
MIN_SIGNAL_HISTORY_DAYS = 7

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
#
# 2 and not 1, measured 2026-08-27 over three snapshots of the committed db:
# OpenSky's arrival estimator leaves `arr_icao` NULL on ~85% of a day's rows
# when the day is asked at age 1, ~40% at age 2, ~25-36% from age 3, and a row
# needs both airports to reach `daily_route`. A lag of 1 therefore judged every
# day at its worst moment and wrote `outage` on days that rescored `ok` a day
# later -- 08-24 went outage(0.369) -> ok(0.933) exactly that way. Those false
# outages cost observed days, and under a 5-in-7 floor a lost day costs a week.
SETTLE_LAG_DAYS = 2

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
