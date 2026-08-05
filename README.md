# GulfWatch

Free-tier monitor for Middle East airline operations during the current
conflict. Tracks whether ~20 carriers are still serving DOH / BAH / DXB and the
wider Gulf, at what weekly frequency, and who is still crossing the FIRs that
EASA tells operators to avoid.

Publishes a static JSON API on GitHub Pages. No servers, no database service,
no paid API keys. Total running cost: zero.

```
OpenSky (OAuth2)  ─┐
adsb.lol / a.live ─┼─►    ETL, on demand   ─► SQLite in repo ─► static JSON ─► report.html
EASA CZIB pages   ─┘      (local or Actions)   (git = history)     + /v1/*.json
```

---

## What it answers

| Question | Endpoint |
|---|---|
| **Has this carrier stopped, when, and for how long?** | `/v1/suspensions.json` |
| Is Gulf Air still flying, and how often? | `/v1/airlines/GFA.json` |
| Who has stopped serving Doha? | `/v1/airports/OTHH.json` → `carriers_absent` |
| What broke in the last week? | `/v1/alerts.json` |
| Is anyone still overflying Tehran FIR? | `/v1/firs.json` |
| Did EASA revise its conflict-zone bulletin? | `/v1/advisories.json` |
| Can I trust today's numbers? | `/v1/health.json` and the `coverage` block in every payload |

---

## Setup

### 1. Get OpenSky credentials (required)

OpenSky retired username/password auth in March 2026. Create an account, go to
**Account → API clients**, create a client, and put the pair into repo
**Settings → Secrets → Actions**:

```
OPENSKY_CLIENT_ID
OPENSKY_CLIENT_SECRET
```

There is no anonymous fallback. OpenSky now answers `403` on the flights
endpoints without a token, so without these two secrets the monitor collects no
flight history at all — FIR sampling and the EASA scrape still run, but the
question this project exists to answer goes unanswered. Feeding a receiver to
OpenSky raises the daily credit allowance considerably if you need more
headroom.

### 2. Build the baseline — do this first

Nothing in this project means anything without a pre-escalation reference
period. "12 flights a week" is not information; "12 against a baseline of 21"
is.

Actions → **backfill-baseline** → Run workflow. Defaults to 2025-11-01 →
2026-01-31.

**This does not finish in one run.** Measured against a real account, ~85
requests exhaust OpenSky's daily allowance, and a 92-day baseline across 13
airports needs ~1200. The harvest is therefore resumable: each 7-day slice is
recorded in `backfill_progress` as it lands, a rate limit stops the run
cleanly, and re-running picks up exactly where it left off. Expect to run it
once a day for about two weeks — or narrow the scope (fewer airports, shorter
window) and finish in a few days.

The baseline is frozen only when every slice is in. A partial harvest is left
unfrozen on purpose: a baseline built from a fraction of the window understates
the routes it did reach several-fold and silently omits the rest, and every
number the project publishes is measured against it.

Locally:

```bash
pip install -r requirements.txt
export OPENSKY_CLIENT_ID=... OPENSKY_CLIENT_SECRET=...
python -m src.backfill --start 2025-11-01 --end 2026-01-31
```

### 3. Turn on Pages

Settings → Pages → Source: **GitHub Actions**. Push once; the `pages` workflow
deploys `public/`. Your API is then at:

```
https://<user>.github.io/<repo>/v1/status.json
```

### 4. Let it run

Nothing runs on a timer. The `ingest` workflow is manual — Actions → **ingest**
→ Run workflow — and so is everything else. Locally the same thing is:

```bash
python -m src.ingest          # pull flights, score coverage, detect stops
python -m src.report          # write public/report.html
```

Deliberate rather than unfinished. Analysis is anchored on the last settled UTC
day, so repeated runs recompute the same answer, and ~85 requests exhaust
OpenSky's daily allowance while one all-airports pass costs ~52. A timer would
spend the budget whether or not anyone was reading. To schedule it anyway, add
a `schedule:` block back to `.github/workflows/ingest.yml`.

### Try it without credentials

```bash
python -m src.demo_seed --wipe    # 120 days of synthetic traffic
python -m src.publish
python -m http.server -d public 8000
```

The demo scenario suspends Middle East Airlines and Royal Jordanian, cuts
Kuwait Airways to a quarter, and thins Gulf Air, Finnair and BA — enough to
exercise every branch of the alerting logic. **Delete `data/gulfwatch.db`
before you go live** so demo rows never reach the published API.

---

## Stop detection

"Did they stop" is not a status, it is an **event** with a start, a duration and
an end. `/v1/suspensions.json` carries those events across runs.

Three scopes, because stopping means different things:

| Scope | Key | Threshold | Meaning |
|---|---|---|---|
| `route` | `QTR\|OTHH\|OMDB` | 7 silent days | one city pair dropped |
| `station` | `QTR\|OMDB` | 10 silent days | the carrier left an airport entirely |
| `region` | `QTR` | 21 silent days | gone from every monitored airport |

A region stop rolls its station and route rows up under it (`superseded_by`), so
the list reads the way a person would write it rather than repeating one event
nine times.

Each event carries:

```json
{
  "carrier": "MEA", "scope": "station", "detail": "OMDB",
  "last_flight_on": "2026-07-14",
  "started_on": "2026-07-15",
  "days_stopped": 20,
  "status": "active",
  "confidence": "corroborated",
  "evidence": [{"source": "news", "title": "...", "url": "...", "stance": "supports"}]
}
```

`/v1/airlines/{code}.json` answers it in one field:

```json
"operating": false,
"stopped": {"any": true, "scope": "region", "since": "2026-07-14",
            "days_stopped": 21, "airports_dropped": ["OBBI","OLBA","OMDB"]}
```

`/v1/airports/{icao}.json` gives the same view from the other side:
`carriers_stopped` and `resumed_recently`.

### Two rules that keep this honest

**Silent days are coverage-gated.** A day when the sensor network was degraded
neither extends a silence streak nor breaks it — it is skipped entirely, and the
detector refuses to open or close any event on a bad-coverage day. Without this,
every ADS-B outage manufactures a fake suspension with a convincing start date.

**ADS-B silence is not a decision.** Three things look identical to a receiver:
the carrier suspended the service, the airport is closed, or we stopped seeing
them. Coverage gating handles the third. `src/corroborate.py` attacks the other
two using Google News RSS (free, no key) and, if you register for the free FAA
NOTAM API, an airport-closure check. Every event is graded:

- `observed` — ADS-B silence only, nobody has confirmed anything
- `corroborated` — reporting agrees they stopped
- `contradicted` — reporting says they are flying. **Trust the reporting, not
  this feed.** Usually means coverage is thinner than the control group showed.

Corroboration is keyword-based on purpose. Its job is to hand you the three
links a human should read and to flag disagreement, not to decide.

### Resumptions

The first observed flight after a stop closes the event, records `resumed_on`
and freezes `days_stopped`. `/v1/suspensions.json → recently_resumed` covers the
last 30 days. Coming back is as newsworthy as leaving, and a monitor that only
tracks departures from normal quietly rots.

Optional: set `FAA_CLIENT_ID` / `FAA_CLIENT_SECRET` (free registration) to
enable the airport-closure check.

---

## Design decisions worth knowing

**Frequency is weekly, never daily.** Many of these routes run 2–4 times a
week. A daily count is mostly day-of-week noise dressed up as a trend. Every
figure is departures per rolling 7 days per `(carrier, origin, destination)`.

**The API is one day behind, deliberately.** Every payload carries `as_of_day`,
and it is yesterday, not today. Today is always structurally incomplete — at
10:00 UTC only 40% of it has happened, and OpenSky publishes with a further lag
on top. Scored against a median of complete days, today reads as an outage on
every single run, which suppresses every alert and freezes the stop detector.
A monitor that has the data and refuses to speak is worse than one that is a
day late. Tune with `SETTLE_LAG_DAYS` in `config.py`.

**Coverage integrity gates everything.** ADS-B coverage comes from volunteer
receivers. A receiver going offline and an airline suspending a route look
identical in the raw data. A control group of carriers that will certainly keep
flying (Emirates, Turkish, Lufthansa, Air France, KLM) is scored against its own
28-day median. Below 0.70 the data is `degraded`; below 0.40 it is an `outage`
and **all statuses become UNKNOWN and all alerts are suppressed**. A monitor
that cries wolf during a sensor outage is worse than no monitor.

**Callsign prefixes, not IATA codes.** ADS-B carries ICAO operator designators.
flynas is `KNE`, not XY. Pegasus is `PGT`, not PC. Saudia is `SVA`. This is the
single most common way these projects produce confidently wrong output.
`tests/test_core.py` guards it.

**GNSS spoofing is filtered out.** The Gulf and Levant see sustained navigation
interference. Spoofed aircraft still transmit ADS-B, but with collapsed NIC/SIL
integrity figures. Positions below the floor are dropped before FIR counting;
`firs.json` reports how many were rejected.

**Suspension needs confirmation.** A route reads `SUSPENDED` only when the whole
7-day window is empty *and* it has been silent for at least 3 consecutive days
*and* coverage is healthy. Otherwise it reads `MINIMAL`.

**Ingest is idempotent.** OpenSky publishes with a lag and backfills late, so
every run re-reads a 48-hour window and upserts on `(icao24, first_seen)`.
Forward-only appending silently loses flights.

**Freight is separated.** Several carriers fly cargo under the passenger
callsign; `cargo_flightnum_min` in `config/carriers.yml` splits them by flight
number range. Adjust per carrier as you verify actual series.

---

## Known limits — read before trusting a number

- **Codeshares and wet leases are invisible.** ADS-B shows the operator, not the
  marketing carrier. A QR-marketed flight on an ACMI partner's aircraft counts
  as the partner.
- **FIR boundaries are approximations.** `config/airports.yml` uses bounding
  boxes, not real ICAO FIR polygons. Fine for trends, wrong for exact counts.
  See below.
- **`estDepartureAirport` is estimated.** OpenSky infers it; short hops and
  poorly covered fields are sometimes wrong or null.
- **Coverage over Iran and Iraq is thin** even in normal times. Low counts there
  are partly a sensor story, not only an avoidance story.
- **FIR sampling is a snapshot, not a census.** Trend the daily figure; do not
  read a single sample as truth.
- Absence of data is never evidence of absence of flights. Every payload says so.

## Upgrading the FIR layer

Replace the `firs:` bounding boxes with real ICAO FIR polygons (several open
GeoJSON sets exist) and swap `_inside()` in `src/firwatch.py` for a
point-in-polygon test — `shapely` is the easy route. Everything else stays.

## Licence and fair use

- **OpenSky Network** — non-commercial use only.
- **adsb.lol** — ODbL 1.0. Redistributing derived data carries attribution and
  share-alike obligations; `publish.py` emits the attribution block in every
  payload for this reason.
- **airplanes.live** — non-commercial, no SLA, 1 request/second. Respected.

This is a personal research tool. It is not an operational decision-making
system and must not be used as one. For actual flight planning the sources are
the AIP, the NOTAM system, and your operator's own risk assessment.

## Layout

```
config/carriers.yml     ICAO prefix registry, tracked/control/cargo flags
config/airports.yml     polled airports + FIR envelopes
src/opensky.py          OAuth2 client, 7-day window splitting, backoff
src/adsb_live.py        adsb.lol → airplanes.live failover, GNSS integrity filter
src/parse.py            callsign → carrier, freight split
src/ingest.py           main ETL
src/backfill.py         one-time baseline harvest and freeze
src/metrics.py          rollups, coverage scoring, classification, alerts
src/suspensions.py      stop/resume event state machine, three scopes
src/corroborate.py      Google News RSS + FAA NOTAM corroboration
src/firwatch.py         FIR overflight sampling
src/advisories.py       EASA CZIB scrape + revision-change detection
src/publish.py          static JSON API generation
public/index.html       dashboard, reads the same JSON
tests/test_core.py      23 tests, including the outage-vs-suspension guard
.github/workflows/      ingest (daily), backfill-baseline (manual), pages
```
