"""Operator report. Run it by hand when you want to know where things stand.

    python -m src.report                 # writes public/index.html, the site front page
    python -m src.report --no-news       # skip the news sweep (faster, offline)
    python -m src.report --days 14       # wider observation window

Four sources, read in that order of authority, because no one of them answers
the question alone:

  ADS-B     what actually flew -- but only where volunteers run receivers.
            Measured: strong over the UAE, Qatar, Bahrain and Jordan, and
            effectively blind over Saudi Arabia, Kuwait, Iraq and Iran.
  Schedule  what the carrier still intends to fly, everywhere, independent of
            receivers. Also the denominator: 719 departures means nothing
            until you know how many were timetabled.
  News      what was announced -- everywhere, but only for carriers big enough
            to be written about. An announcement is not an observation.
  Web       last resort, only where the other three are silent. Never changes
            a state; attaches a cited note so the reader can go and look.

Observation outranks announcement throughout. A carrier seen flying is flying
whatever the press says; a carrier we cannot see but whose timetable still
stands is unknown, not stopped -- turning a coverage gap into a suspension is
the failure this report is built to avoid.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import (advisories, classify, config, db, firwatch, flightboard,
               metrics, schedules, websearch)
from .corroborate import _news

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("gulfwatch.report")

STOPPED = re.compile(
    # "suspen\w*" not "suspend\w*": the noun is suspenSion, so the -d stem
    # silently misses every "Extends Flight Suspensions" headline.
    r"\b(suspen\w*|halt\w*|cancel\w*|stops? flights|pause\w*|grounded|"
    r"withdraw\w*|axe[sd]?|scrap(?:s|ped)?|cuts?|"
    r"pulls? (?:the )?plug|drops? (?:its )?(?:route|flights|service))\b", re.I)
RESUMED = re.compile(
    r"\b(resum\w*|restart\w*|restor\w*|reinstat\w*|returns? to|relaunch\w*)\b", re.I)

# Carrier names that collapse to a country, a nationality or the conflict
# itself once the "Airways"/"Airlines" suffix comes off. These keep their full
# name and nothing shorter.
AMBIGUOUS_SHORT = {
    "qatar", "british", "american", "japan", "kuwait", "saudi", "china",
    "middle east", "egypt", "turkish", "gulf", "emirates", "oman", "royal",
}

# How old a headline may be and still describe the situation now. Unbounded,
# Google happily answers with February suspension notices in August.
NEWS_MAX_AGE_DAYS = 30

# Web searches per run. Only carriers with no ADS-B, no schedule and no
# headline reach this, and there are rarely more than a handful.
MAX_WEB_SEARCHES = 6

# Coverage-passing days needed before an observed-vs-scheduled percentage is
# worth printing. Fewer than this and the report shows the two raw numbers and
# withholds the ratio.
#
# Shared with the JSON API rather than duplicated. This rule was written here
# first and metrics.route_report went without it for months, which is how the
# API came to publish Qatar Airways at 23% of baseline off two observed days
# while this report withheld the same figure as unsupportable. One constant,
# one answer.
MIN_RATIO_DAYS = config.MIN_OBSERVED_DAYS

# Words that mean the article is about the region rather than this carrier.
GENERIC = re.compile(r"\b(which airlines|airlines (?:have|suspend|resume|cancel)|"
                     r"list of|roundup|factbox)\b", re.I)

REGION_WORDS = re.compile(r"\b(middle east|gulf|arabian|persian)\b", re.I)


def _region_tied(h: dict) -> bool:
    """Is this headline about the airspace we watch?

    A headline only gets to move a carrier's state if it is. The model is asked
    what a headline says about a carrier and it answers that question even when
    the headline is about somewhere else entirely: "Air China launches regular
    flights on the Beijing - Bishkek route" came back as `stopped`, with the
    reason "route launch not mentioned", and because Air China is neither seen
    nor timetabled here that single answer published it as STOPPED. A US IT
    outage did the same for American Airlines.

    Tied means: the classifier bound it to one of our airports, or -- when
    there was no classifier answer -- the headline literally names one of our
    cities or the region. The publisher suffix Google appends is cut off first,
    or every story from Gulf News and timeoutriyadh.com would qualify.
    """
    if h.get("airports"):
        return True
    text = h.get("title", "").rsplit(" - ", 1)[0].lower()
    if REGION_WORDS.search(text):
        return True
    return any(a["city"].lower() in text for a in config.airports().values())


# --- Data ------------------------------------------------------------------

def activity(conn, days: int) -> dict:
    """Per carrier: which monitored airports we actually saw them at."""
    since = (metrics.reference_day() - timedelta(days=days - 1)).isoformat()
    until = metrics.reference_day().isoformat()
    rows = conn.execute(
        """SELECT carrier, dep_icao, arr_icao, COUNT(*) n
           FROM flight
           WHERE is_freight = 0 AND dep_date BETWEEN ? AND ?
             AND carrier IS NOT NULL
           GROUP BY carrier, dep_icao, arr_icao""",
        (since, until)).fetchall()

    airports = set(config.airports())
    seen: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for icao in (r["dep_icao"], r["arr_icao"]):
            if icao in airports:
                seen[r["carrier"]][icao] += r["n"]
    return {"since": since, "until": until, "seen": seen}


def airport_view(conn, days: int, tracked: dict) -> list[dict]:
    """Per airport: is anything still moving there, and on whose word.

    The carrier table answers "is this airline flying". This answers the other
    half — "is this airport still being served" — which is otherwise buried in
    a column of three-letter codes.
    """
    since = (metrics.reference_day() - timedelta(days=days - 1)).isoformat()
    until = metrics.reference_day().isoformat()
    seen: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    legs: dict[str, int] = defaultdict(int)
    for r in conn.execute(
            """SELECT carrier, dep_icao, arr_icao, COUNT(*) n FROM flight
               WHERE is_freight = 0 AND dep_date BETWEEN ? AND ?
                 AND carrier IS NOT NULL
               GROUP BY carrier, dep_icao, arr_icao""", (since, until)):
        for icao in (r["dep_icao"], r["arr_icao"]):
            if icao in config.airports():
                legs[icao] += r["n"]
                if r["carrier"] in tracked:
                    seen[icao][r["carrier"]] += r["n"]

    sched_w: dict[str, int] = defaultdict(int)
    sched_c: dict[str, set] = defaultdict(set)
    # Per carrier, not just the count: on the airports ADS-B cannot see, the
    # timetable is the only thing that can name who is still serving them.
    sched_by: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in conn.execute(
            "SELECT dep_iata, arr_iata, carrier, weekly FROM route_schedule "
            "WHERE weekly > 0"):
        for iata in (r["dep_iata"], r["arr_iata"]):
            sched_w[iata] += r["weekly"]
            sched_c[iata].add(r["carrier"])
            if r["carrier"] in tracked:
                sched_by[iata][r["carrier"]] += r["weekly"]

    out = []
    for icao, cfg in config.airports().items():
        iata = cfg["iata"]
        n, sw = legs.get(icao, 0), sched_w.get(iata, 0)
        if n:
            state = "acik"
        elif sw:
            state = "tarifeli"
        else:
            state = "durgun"
        out.append({
            "icao": icao, "iata": iata, "city": cfg["city"],
            "country": cfg.get("country"), "legs": n, "state": state,
            "carriers_seen": len(seen.get(icao, ())),
            "sched_weekly": sw,
            "carriers_sched": len({c for c in sched_c.get(iata, ()) if c in tracked}),
            # Who, not how many. Observed traffic where we have it, the
            # timetable where we do not -- the row's state says which.
            "who": sorted(seen[icao].items(), key=lambda kv: -kv[1]) if n
                   else sorted(sched_by.get(iata, {}).items(), key=lambda kv: -kv[1]),
        })
    rank = {"durgun": 0, "tarifeli": 1, "acik": 2}
    out.sort(key=lambda a: (rank[a["state"]], -a["legs"]))
    return out


def observed_vs_scheduled(conn, days: int) -> dict[str, dict]:
    """Departures per week between monitored airports: flown vs timetabled.

    The denominator the report was missing. "719 departures observed" says
    nothing on its own; "719 against 1014 scheduled" is the answer to whether
    a carrier has cut back, and unlike a historical baseline it is available
    today and describes what the airline currently intends to fly.

    Both sides are restricted to monitored-to-monitored city pairs, because
    that is the only universe both sources cover. Counting a Dubai-London
    departure ADS-B saw against a timetable that was never asked about London
    would understate every carrier with long-haul routes.
    """
    iata = {k: v["iata"] for k, v in config.airports().items()}
    since = (metrics.reference_day() - timedelta(days=days - 1)).isoformat()
    until = metrics.reference_day().isoformat()

    # Divide by the days that actually hold data, not by the width of the
    # window. Ingest is daily and the history is short, so a nominal 7-day
    # window can contain two days of flights -- scaling those to a week over
    # seven understated every carrier threefold and would have published
    # Qatar Airways at 11% of its timetable instead of about 60%.
    # Only days the coverage gate passed, and only if there are enough of
    # them. With two or three days of thin history a weekly figure is an
    # extrapolation with error wider than the signal -- Qatar Airways came out
    # at 17% of its timetable on a base whose complete days you can count on
    # one hand. Below the floor the report shows both raw numbers and no
    # percentage, which is the honest shape of "not enough data yet".
    # A day the network calls `ok` is still no use for a percentage if the one
    # fetch that could have caught these legs came back thin: the numerator
    # drops and the timetable does not, so the shortfall renders as a cut.
    # Asked per city pair and per direction, because that is the grain at
    # which a leg is either visible or not. See
    # metrics.airport_side_coverage.
    ref = metrics.reference_day()
    cov = metrics.coverage_map(conn)
    ok_days = set(metrics.observed_days(cov, ref, days))
    ap_cov = metrics.airport_side_coverage(conn, ref, days)

    def visible(dep: str, arr: str) -> set[str]:
        return ok_days & (ap_cov.get((dep, "dep"), set())
                          | ap_cov.get((arr, "arr"), set()))

    # Which pairs carry a percentage at all. Both ends are monitored here, so
    # either fetch can vouch for a day, and a pair that clears the floor is
    # scaled by ITS days rather than by the network's.
    per_pair: dict[tuple, dict[str, int]] = defaultdict(dict)
    for r in conn.execute(
            """SELECT carrier, dep_icao, arr_icao, dep_date, COUNT(*) n
               FROM flight
               WHERE is_freight = 0 AND dep_date BETWEEN ? AND ?
                 AND carrier IS NOT NULL AND dep_icao <> arr_icao
               GROUP BY carrier, dep_icao, arr_icao, dep_date""", (since, until)):
        if r["dep_icao"] in iata and r["arr_icao"] in iata:
            per_pair[(r["carrier"], r["dep_icao"], r["arr_icao"])][r["dep_date"]] = r["n"]

    icao_of = {v: k for k, v in iata.items()}
    sch_pair: dict[tuple, int] = defaultdict(int)
    for r in conn.execute(
            "SELECT carrier, dep_iata, arr_iata, weekly FROM route_schedule"):
        dep, arr = icao_of.get(r["dep_iata"]), icao_of.get(r["arr_iata"])
        if dep and arr:
            sch_pair[(r["carrier"], dep, arr)] += r["weekly"] or 0

    obs: dict[str, float] = defaultdict(float)
    sch: dict[str, int] = defaultdict(int)
    sch_all: dict[str, int] = defaultdict(int)
    pairs_seen: dict[str, int] = defaultdict(int)
    pairs_all: dict[str, int] = defaultdict(int)
    for key in set(per_pair) | set(sch_pair):
        code, dep, arr = key
        planned = sch_pair.get(key, 0)
        sch_all[code] += planned
        pairs_all[code] += 1
        vis = visible(dep, arr)
        if len(vis) < MIN_RATIO_DAYS:
            # Dropped from BOTH sides. Leaving the timetable in while the
            # sightings fall out is what published Emirates at 8% of its
            # own schedule on 2026-08-20: seven of the fifteen monitored
            # airports return no ADS-B at all, so their routes were pure
            # denominator.
            continue
        flown = sum(n for d, n in per_pair.get(key, {}).items() if d in vis)
        obs[code] += flown * 7 / len(vis)
        sch[code] += planned
        pairs_seen[code] += 1

    out = {}
    for code in set(obs) | set(sch_all):
        planned = sch[code]
        share = (planned / sch_all[code]) if sch_all[code] else 0.0
        # The same test the API ratio applies: a percentage built on a sliver
        # of the timetable is a statement about the sliver.
        usable = planned > 0 and share >= config.MIN_COMPARABLE_SHARE
        out[code] = {"observed": round(obs[code], 1), "scheduled": planned,
                     "scheduled_total": sch_all[code],
                     "ratio": (round(obs[code] / planned, 2) if usable else None),
                     "pairs": pairs_seen[code], "pairs_total": pairs_all[code],
                     "share": round(share, 3), "trustworthy": usable}
    return out


def baseline_weekly(conn) -> dict[str, float]:
    """Carrier -> baselined departures per week, if a baseline exists at all."""
    rows = conn.execute(
        "SELECT carrier, SUM(weekly_freq) w FROM baseline GROUP BY carrier"
    ).fetchall()
    return {r["carrier"]: r["w"] for r in rows}


def _aliases(name: str) -> set[str]:
    """What a headline must literally say for us to attribute it to this carrier.

    The full name, not the first word. Matching on the first word attributes
    "Airspace closed, airlines halt flights" to Air Algerie, because "air" is
    inside "airspace" -- which then reads as a carrier we never saw having
    stopped, on evidence about nobody in particular.

    Nor is the bare first half of the name enough: dropping "Airways" from
    "Qatar Airways" leaves "qatar", which matches every headline about the
    country, and "Middle East Airlines" collapses to "middle east", which
    matches the entire conflict.

    So: the full name always, plus the short form only when what is left is a
    distinctive brand rather than a place. "Etihad Airways" -> "etihad" is
    safe and catches the many headlines that write it that way; "Qatar
    Airways" -> "qatar" is not.
    """
    base = re.sub(r"\s*\(.*?\)", "", name).strip().lower()
    out = {base}
    for suffix in (" airways", " airlines"):
        if base.endswith(suffix):
            short = base[:-len(suffix)]
            if short not in AMBIGUOUS_SHORT:
                out.add(short)
    return out


def _age_days(published: str) -> int | None:
    try:
        return (datetime.now(tz=timezone.utc)
                - parsedate_to_datetime(published)).days
    except (TypeError, ValueError):
        return None


def carrier_news(name: str, limit: int = 2,
                 max_age: int = NEWS_MAX_AGE_DAYS) -> list[dict] | None:
    """Recent headlines that name this carrier. None if the source did not answer.

    Two filters, and the project was wrong without either.

    Recency: an unbounded query answers "is Saudia flying" with a suspension
    notice from five months ago. Measured, the top results were 134 to 157 days
    old and were setting today's verdict. `when:Nd` bounds it at the source and
    the publication date is re-checked here, because the operator ignores it
    often enough to matter.

    Attribution: Google answers a carrier query with regional round-ups far
    more often than with anything about the carrier, so a headline that does
    not name it is dropped rather than credited to it.
    """
    items = _news(f'"{name}" flights suspended OR cancelled OR resumed '
                  f'Middle East OR Gulf OR Dubai OR Doha OR Qatar '
                  f'when:{max_age}d', limit=12)
    if items is None:
        return None
    keys = _aliases(name)
    out = []
    for it in items:
        title = it["title"]
        low = title.lower()
        if not any(k in low for k in keys) or GENERIC.search(title):
            continue
        age = _age_days(it.get("published"))
        if age is None or age > max_age:
            continue
        it["age_days"] = age
        it["signal"] = ("resumed" if RESUMED.search(title)
                        else "stopped" if STOPPED.search(title) else "mentioned")
        out.append(it)
    out.sort(key=lambda x: x["age_days"])
    return out[:limit]


def _remember(conn, kind: str, subject: str, items: list[dict]) -> None:
    """Keep the last set the source did answer with."""
    conn.execute(
        """INSERT OR REPLACE INTO headline_cache (subject, kind, fetched_at, payload)
           VALUES (?,?,?,?)""",
        (subject, kind,
         datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
         json.dumps(items)))
    conn.commit()


def _recall(conn, kind: str, subject: str) -> dict | None:
    """The last answered set for this subject, with its age in days.

    Shown when the source is down, never fed to verdict(). A headline that
    could not be re-checked today is a link worth reading, not evidence of
    what is true today -- deciding a carrier is stopped off a cached headline
    is the same mistake as deciding it off a five-month-old one.
    """
    row = conn.execute(
        "SELECT fetched_at, payload FROM headline_cache WHERE subject=? AND kind=?",
        (subject, kind)).fetchone()
    if not row:
        return None
    items = json.loads(row["payload"])
    if not items:
        return None
    age = (datetime.now(tz=timezone.utc)
           - datetime.fromisoformat(row["fetched_at"])).days
    return {"items": items, "age_days": age}


def blind_news(conn, max_age: int = NEWS_MAX_AGE_DAYS,
               per_airport: int = 3) -> list[dict]:
    """Headlines about the airports no receiver can see.

    Seven of fifteen airports return zero ADS-B -- measured, and confirmed
    against the keyless aggregators too, which share the same volunteer
    receiver geography. For those, "did anyone stop flying here" cannot be
    answered by observation at all, and the timetable only says what is
    still *planned*.

    So we ask the press directly, by airport rather than by carrier. A
    carrier-name query returns regional round-ups; an airport query returns
    the thing itself -- "Etihad, Emirates, Air Arabia, flydubai cancel Kuwait
    flights" names four carriers in one headline, which no per-carrier query
    surfaced.

    This decides nothing, deliberately. It hands over dated, linked headlines
    for a human to read, the same contract corroborate.py works under.
    """
    out = []
    for icao in schedules.BLIND:
        cfg = next((c for i, c in config.airports().items()
                    if c["iata"] == icao or i == icao), None)
        if not cfg:
            continue
        city = cfg["city"]
        items = _news(f'"{city}" airport flights suspended OR cancelled '
                      f'OR resumed OR halted when:{max_age}d', limit=8)
        if items is None:
            stale = _recall(conn, "airport", icao)
            LOG.info("%s (%s): source did not answer%s", icao, city,
                     f" (showing {len(stale['items'])} cached)" if stale else "")
            out.append({"iata": icao, "city": city, "items": [],
                        "failed": True, "stale": stale})
            continue
        hits = []
        for it in items:
            if city.lower() not in it["title"].lower():
                continue          # regional round-up that never names the place
            age = _age_days(it.get("published"))
            if age is None or age > max_age:
                continue
            it["age_days"] = age
            it["signal"] = ("resumed" if RESUMED.search(it["title"])
                            else "stopped" if STOPPED.search(it["title"])
                            else "mentioned")
            hits.append(it)
        hits.sort(key=lambda x: x["age_days"])
        LOG.info("%s (%s): %s headlines", icao, city, len(hits))
        kept = hits[:per_airport]
        _remember(conn, "airport", icao, kept)
        out.append({"iata": icao, "city": city, "items": kept,
                    "failed": False, "stale": None})
    return out


def verdict(seen_at: dict, sched: dict | None,
            news: list[dict] | None) -> tuple[str, str]:
    """(durum, gerekçe) — üç kaynaktan.

    Sıralama: gözlem > tarife > haber. Uçtuğu görülen bir havayolu, basın ne
    derse desin uçuyordur; görülmeyen ama tarifesi duran bir havayolu için
    'durdurdu' demek, kapsama boşluğunu kesintiye dönüştürmek olur.

    `news=None` "basına sorulamadı" demektir, "basında haber yok" değil. Fark
    yalnızca üç kaynağın da sessiz kaldığı satırda görünür: orada gerekçe
    kaynağın cevap vermediğini söylemek zorunda, yoksa rapor sormadığı bir
    soruya cevap vermiş gibi okunur.
    """
    haber_var = news is not None
    news = news or []
    # Only headlines about this region decide anything -- see _region_tied.
    # The rest stay in the table as links, tagged for what they are.
    kesinti = any(x["signal"] == "stopped" and _region_tied(x) for x in news)
    vurulan = sorted({a for x in news if x["signal"] == "stopped"
                      for a in (x.get("airports") or [])})
    nere = f" ({', '.join(vurulan)})" if vurulan else ""
    haftalik = (sched or {}).get("weekly") or 0
    nerede = ", ".join(sorted((sched or {}).get("airports", {})))

    if seen_at:
        n = sum(seen_at.values())
        if kesinti:
            return "partial", (f"{n} sefer havada görüldü, ancak basın "
                               f"hat kesintisi bildiriyor{nere}")
        return "flying", f"{n} sefer havada görüldü, hat kesintisi bildirilmedi"

    if haftalik:
        if kesinti:
            return "partial", (f"kapsamamız dışında, ama tarifesinde haftada "
                               f"{haftalik} sefer duruyor ({nerede}); basın bazı "
                               f"hatlarının kesildiğini bildiriyor")
        return "scheduled", (f"ADS-B kapsamamız dışında uçuyor; tarifesinde "
                             f"haftada {haftalik} sefer duruyor ({nerede})")

    if kesinti:
        return "stopped", ("ne havada görüldü ne tarifesinde sefer var, üstelik "
                           "basın durdurduğunu bildiriyor — üç kaynak da aynı yönde")
    if any(x["signal"] == "resumed" and _region_tied(x) for x in news):
        return "unknown", ("görülmedi ve tarifesinde sefer yok, ama basın yeniden "
                           "başladığını yazıyor — çelişki")
    if not haber_var:
        return "unknown", ("ne havada görüldü ne tarifesinde sefer var; basın "
                           "kaynağı bu çalıştırmada yanıt vermedi, yani basına "
                           "sorulamadı — bu 'durdurdu' demek değildir")
    return "unknown", ("hiçbir kaynakta izine rastlanmadı — bu 'durdurdu' "
                       "demek değildir")


def enrich(conn, code: str, name: str, news: list[dict]) -> None:
    """Let the model re-read each headline. Regex stays as the fallback."""
    for h in news:
        got = classify.classify(conn, code, name, h)
        if not got:
            h["source"] = "regex"
            continue
        h["source"] = "llm"
        h["airports"] = got["airports"]
        h["why_llm"] = got["why"]
        h["signal"] = {"stopped": "stopped", "resumed": "resumed",
                       "unaffected": "unaffected",
                       "unclear": "mentioned",
                       "irrelevant": "irrelevant"}[got["action"]]


def diff_since_last(conn, rows: list[dict], coverage_ok: bool) -> dict:
    """What moved since the previous run, and record today for the next one.

    A snapshot answers "where do things stand". An operator watching a
    conflict needs the other question — what changed — and the history to
    answer it is already in the database.

    Airport gains and losses are only compared between two days that both
    passed the coverage gate. On 2026-08-10 the OpenSky allowance was spent
    before the run, so the seven-day window lost a day off its tail and gained
    nothing: six carriers were reported as having "kayboldu" from Bahrain and
    Doha on a day nobody had looked. A carrier's state can still change on a
    blind day — the press moves it — but an airport disappearing from a window
    that stopped being filled is an artefact of the window, not a departure
    that stopped.
    """
    today = metrics.reference_day().isoformat()
    prev_day = conn.execute(
        "SELECT MAX(day) d FROM report_state WHERE day < ?", (today,)).fetchone()["d"]
    ok_days = {r["day"] for r in conn.execute(
        "SELECT day FROM coverage WHERE verdict = 'ok'")}
    compare_airports = coverage_ok and prev_day in ok_days

    changes: list[dict] = []
    if prev_day:
        before = {r["carrier"]: r for r in conn.execute(
            "SELECT carrier, state, airports FROM report_state WHERE day=?",
            (prev_day,))}
        for r in rows:
            was = before.get(r["code"])
            if not was:
                continue
            now_ap = set(r["seen_at"])
            was_ap = {a for a in (was["airports"] or "").split(",") if a}
            iata = {k: v["iata"] for k, v in config.airports().items()}
            if was["state"] != r["state"]:
                changes.append({"carrier": r["code"], "name": r["name"],
                                "kind": "durum", "was": STATE_LABEL[was["state"]],
                                "now": STATE_LABEL[r["state"]]})
            if not compare_airports:
                continue
            gone = sorted(iata.get(a, a) for a in was_ap - now_ap)
            new = sorted(iata.get(a, a) for a in now_ap - was_ap)
            if gone:
                changes.append({"carrier": r["code"], "name": r["name"],
                                "kind": "kayboldu", "was": ", ".join(gone), "now": ""})
            if new:
                changes.append({"carrier": r["code"], "name": r["name"],
                                "kind": "geri döndü", "was": "", "now": ", ".join(new)})

    conn.executemany(
        """INSERT OR REPLACE INTO report_state (day, carrier, state, legs, airports)
           VALUES (?,?,?,?,?)""",
        [(today, r["code"], r["state"], r["legs"], ",".join(sorted(r["seen_at"])))
         for r in rows])
    conn.commit()
    return {"since": prev_day, "changes": changes,
            "airports_compared": compare_airports}


def collect(days: int, with_news: bool, news_days: int = NEWS_MAX_AGE_DAYS,
            use_llm: bool = True) -> dict:
    conn = db.connect()
    use_llm = use_llm and classify.available()
    ref = metrics.reference_day()
    act = activity(conn, days)
    base = baseline_weekly(conn)
    sched_by_carrier = schedules.by_carrier(conn)
    ratios = observed_vs_scheduled(conn, days)
    carriers = config.tracked_carriers()

    rows = []
    news_failures = 0
    for code, cfg in sorted(carriers.items(), key=lambda kv: kv[1]["name"]):
        news, stale = [], None
        if with_news:
            news = carrier_news(cfg["name"], max_age=news_days)
            if news is None:
                news_failures += 1
                stale = _recall(conn, "carrier", code)
                LOG.info("%s: source did not answer%s", code,
                         f" (showing {len(stale['items'])} cached)" if stale else "")
            else:
                if use_llm:
                    enrich(conn, code, cfg["name"], news)
                _remember(conn, "carrier", code, news)
                LOG.info("%s: %s headlines", code, len(news))
            time.sleep(1.2)          # be polite to Google News
        seen_at = dict(act["seen"].get(code, {}))
        sched = sched_by_carrier.get(code)
        # `stale` deliberately does not reach verdict(): it is shown, not
        # counted. news is still None here, so the row reads "sorulamadı".
        state, why = verdict(seen_at, sched, news)
        rows.append({"code": code, "name": cfg["name"],
                     "iata": cfg.get("iata"), "country": cfg.get("country"),
                     "seen_at": seen_at, "legs": sum(seen_at.values()),
                     "baseline": base.get(code), "news": news or [],
                     "stale": stale if news is None else None,
                     "news_failed": with_news and news is None,
                     "sched": sched, "ratio": ratios.get(code),
                     "state": state, "why": why})

    # Last resort, and only for the rows that would otherwise say nothing at
    # all. A search costs far more than a headline lookup and is weaker
    # evidence, so it is spent where the report is genuinely blank.
    silent = [r for r in rows if r["state"] == "unknown"]
    if use_llm and silent:
        agent_id = websearch.agent()
        for r in silent[:MAX_WEB_SEARCHES]:
            got = websearch.resolve(conn, r["code"], r["name"], agent_id)
            if got:
                r["note"] = got
            # No gap here on purpose: the 3s one added on 2026-08-06 never
            # worked, and websearch now paces itself off the budget the API
            # reports in its own headers.

    coverage = metrics.score_coverage(conn, ref)
    delta = diff_since_last(conn, rows, coverage["verdict"] == "ok")
    # How many days of this window we actually looked at, from the definition
    # metrics and suspensions already share. It governs whether the ratio
    # column exists, how the weekly figures are scaled, and what a silent
    # carrier means -- so it belongs at the top of the page rather than in a
    # footnote under one table.
    observed = metrics.observed_days(
        metrics.coverage_map(conn), ref, metrics.WINDOW_DAYS)

    return {
        "generated_at": datetime.now(tz=timezone.utc),
        "delta": delta,
        "as_of_day": ref.isoformat(),
        "window": (act["since"], act["until"]),
        "days": days,
        "coverage": coverage,
        "observed_days": len(observed),
        "baseline_ready": bool(base),
        "carriers": rows,
        "airports": config.airports(),
        "carriers_cfg": carriers,
        "airport_view": airport_view(conn, days, carriers),
        "blind_news": blind_news(conn, news_days) if with_news else [],
        "boards": flightboard.by_airport(conn),
        "firs": firwatch.summary(conn, days=7),
        "schedule_coverage": schedules.coverage(conn),
        "advisories": advisories.current(conn),
        "with_news": with_news,
        "news_days": news_days,
        "news_failures": news_failures,
        "news_asked": len(rows) if with_news else 0,
        "use_llm": use_llm,
    }


# --- Render ----------------------------------------------------------------

STATE_LABEL = {"flying": "Uçuyor", "partial": "Kısmen kesti",
               "scheduled": "Tarifede var", "stopped": "Durdurdu",
               "unknown": "Bilinmiyor"}

SIGNAL_LABEL = {"stopped": "hat kesintisi", "resumed": "yeniden başladı",
                "unaffected": "etkilenmedi", "mentioned": "ilgili",
                "irrelevant": "bu bölgeyle ilgisiz"}

# One word, one meaning. "Kesinti" is what an AIRLINE did to a route, and it is
# never what happened to our receivers -- the whole point of the page is that
# those two are different, and it read as one word for both. The day's own
# state gets the guide's three phrases verbatim instead of the raw enum value,
# which was printing "ok" in the middle of a Turkish sentence.
COV_LABEL = {"ok": "bakıldı, veri sağlam",
             "outage": "bakıldı ama alıcılar zayıftı",
             None: "bu güne hiç bakılmadı"}

CSS = """
:root{
  --ink:#10161D; --ink-2:#3C4854; --ink-3:#6B7887;
  --bg:#F4F6F8; --panel:#FFFFFF; --rule:#DDE3E9;
  --accent:#2D6A9F; --accent-soft:#E7EFF6;
  --flying:#2F7D6B; --partial:#B0761C; --stopped:#B0433A; --unknown:#7A8794;
  --scheduled:#2D6A9F; --restricted:#B4145F;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{ --ink:#E4EAF0; --ink-2:#A9B5C2; --ink-3:#76838F;
    --bg:#0F141A; --panel:#161D25; --rule:#25303B;
    --accent:#6FA8D6; --accent-soft:#1B2733;
    --flying:#5FB89F; --partial:#D9A445; --stopped:#DE7268; --unknown:#76838F;
    --scheduled:#6FA8D6; --restricted:#E8608F; }
}
:root[data-theme="dark"]{ --ink:#E4EAF0; --ink-2:#A9B5C2; --ink-3:#76838F;
  --bg:#0F141A; --panel:#161D25; --rule:#25303B;
  --accent:#6FA8D6; --accent-soft:#1B2733;
  --flying:#5FB89F; --partial:#D9A445; --stopped:#DE7268; --unknown:#76838F;
    --scheduled:#6FA8D6; --restricted:#E8608F; }
:root[data-theme="light"]{ --ink:#10161D; --ink-2:#3C4854; --ink-3:#6B7887;
  --bg:#F4F6F8; --panel:#FFFFFF; --rule:#DDE3E9;
  --accent:#2D6A9F; --accent-soft:#E7EFF6;
  --flying:#2F7D6B; --partial:#B0761C; --stopped:#B0433A; --unknown:#7A8794;
  --scheduled:#2D6A9F; --restricted:#B4145F; }

*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:40px 24px 72px;
  display:flex;flex-direction:column;gap:40px}
.prose{max-width:64ch}
h1,h2,h3{text-wrap:balance;margin:0}
h1{font-family:var(--mono);font-size:clamp(21px,3.4vw,29px);font-weight:600;
  letter-spacing:.10em;text-transform:uppercase}
/* The masthead the dashboard has carried since the start. Now that the report
   is the front page, it wears the same one. */
.brand{
  font-family:"Barlow Condensed",var(--sans);font-weight:600;
  font-size:clamp(38px,7vw,64px);letter-spacing:.02em;line-height:.92;
  text-transform:uppercase;margin:0;
}
.brand span{color:var(--restricted)}
.deck{font-family:var(--mono);font-size:clamp(13px,1.8vw,16px);font-weight:600;
  letter-spacing:.10em;text-transform:uppercase;color:var(--ink-2);margin-top:2px}
h2{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.16em;
  text-transform:uppercase;color:var(--ink-3);
  padding-bottom:8px;border-bottom:1px solid var(--rule)}
h3{font-size:16px;font-weight:600}
p{margin:0}
a{color:var(--accent)}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--ink-3);
  display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
.sibling{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}
.sibling:hover,.sibling:focus-visible{border-bottom-color:var(--accent)}
.sub{color:var(--ink-2);margin-top:10px}
section{display:flex;flex-direction:column;gap:16px}
header{display:flex;flex-direction:column;gap:6px;
  padding-bottom:24px;border-bottom:2px solid var(--ink)}

.band{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);border-radius:3px;overflow:hidden}
.stat{background:var(--panel);padding:14px 16px;display:flex;
  flex-direction:column;gap:3px}
.stat .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ink-3)}
.stat .v{font-family:var(--mono);font-size:23px;font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1.2}
.stat .n{font-size:12px;color:var(--ink-3)}

/* The day's coverage used to sit in the band as a bare "1.5" beside five
   carrier counts -- a ratio wearing the same type as a headcount, which is
   unreadable by design. It gets its own strip and says it in words. */
.cov-line{font-size:12.5px;color:var(--ink-3);margin:10px 0 0;max-width:78ch}
.cov{margin-top:10px;background:var(--panel);border:1px solid var(--rule);
  border-radius:3px;padding:14px 16px;display:flex;gap:14px;
  align-items:flex-start;flex-wrap:wrap}
.cov p{font-size:13.5px;color:var(--ink-2);max-width:70ch;margin:0}

/* Four units on one page, none of them addable to another. The tables label
   every cell, and this is where the labels are defined once. */
.glossary{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);
  border-radius:3px;margin-top:12px}
.glossary>div{background:var(--panel);padding:12px 14px}
.glossary .u{font-family:var(--mono);font-size:12px;font-weight:600;
  color:var(--accent);display:block;margin-bottom:4px}
.glossary p{font-size:12.5px;color:var(--ink-2);margin:0}

.notice{border-left:3px solid var(--partial);background:var(--panel);
  padding:14px 18px;border-radius:0 3px 3px 0;display:flex;
  flex-direction:column;gap:6px}
.notice .t{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--partial)}
.notice p{font-size:14px;color:var(--ink-2);max-width:72ch}

.scroll{overflow-x:auto;border:1px solid var(--rule);border-radius:3px;
  background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:14px}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--ink-3);text-align:left;font-weight:600;
  padding:11px 14px;border-bottom:1px solid var(--rule);white-space:nowrap;
  background:var(--panel);position:sticky;top:0}
td{padding:11px 14px;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:none}
.code{font-family:var(--mono);font-weight:600;letter-spacing:.04em}
.name{font-weight:500}
.meta{font-size:12px;color:var(--ink-3);font-family:var(--mono)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
.at{font-family:var(--mono);font-size:12px;color:var(--ink-2);
  word-spacing:.2em}
.why{font-size:12.5px;color:var(--ink-3);max-width:38ch}

.pill{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);
  font-size:11px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;
  padding:3px 9px;border-radius:2px;white-space:nowrap;
  border:1px solid currentColor}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
.s-flying{color:var(--flying)} .s-partial{color:var(--partial)}
.s-stopped{color:var(--stopped)} .s-unknown{color:var(--unknown)}
.s-scheduled{color:var(--scheduled)}
.s-unaffected{color:var(--flying)}
.s-mentioned{color:var(--unknown)}
.s-irrelevant{color:var(--unknown)}
tr.r-stopped td{background:color-mix(in srgb,var(--stopped) 6%,transparent)}
tr.r-partial td{background:color-mix(in srgb,var(--partial) 6%,transparent)}

.heads{display:flex;flex-direction:column;gap:5px;margin-top:8px}
.head{font-size:12.5px;line-height:1.4}
.head a{text-decoration:none;border-bottom:1px solid var(--rule)}
.head a:hover{border-bottom-color:var(--accent)}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;padding:1px 5px;border-radius:2px;
  border:1px solid currentColor;margin-right:6px}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:14px 16px;display:flex;flex-direction:column;gap:7px}
.card .hd{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.card .fir{font-family:var(--mono);font-weight:600;letter-spacing:.05em}
.card .cnt{font-family:var(--mono);font-size:19px;font-weight:600;
  font-variant-numeric:tabular-nums}
.card .place{font-size:12.5px;color:var(--ink-3)}
.card .who{font-family:var(--mono);font-size:11.5px;color:var(--ink-2);
  line-height:1.7;word-spacing:.15em}
.watch{color:var(--stopped);font-family:var(--mono);font-size:10px;
  letter-spacing:.1em;text-transform:uppercase}

.notice-inline{border-left:3px solid var(--stopped);
  background:color-mix(in srgb,var(--stopped) 8%,transparent);
  padding:10px 14px;font-size:13.5px;max-width:78ch}

.card.stale{opacity:.75}
ul.blind-news{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:9px}
ul.blind-news li{font-size:13.5px;line-height:1.45}
ul.blind-news a{text-decoration:none;border-bottom:1px solid transparent}
ul.blind-news a:hover,ul.blind-news a:focus-visible{border-bottom-color:var(--accent)}
ul.blind-news .meta{display:block;margin-top:2px}

.who-list{display:flex;flex-wrap:wrap;gap:4px;max-width:34ch}
.who-chip{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;
  padding:1px 5px;border:1px solid var(--rule);border-radius:2px;
  color:var(--ink-2);background:var(--bg);cursor:help}

dl.src{display:grid;grid-template-columns:auto 1fr;gap:7px 18px;font-size:13.5px;
  margin:0}
dl.src dt{font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-3);white-space:nowrap;padding-top:3px}
dl.src dd{margin:0;color:var(--ink-2)}
footer{border-top:1px solid var(--rule);padding-top:20px;font-size:12.5px;
  color:var(--ink-3);max-width:72ch;display:flex;flex-direction:column;gap:8px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (max-width:640px){ .why{display:none} }
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _gun(n: int) -> str:
    return "bugün" if n == 0 else "dün" if n == 1 else f"{n} gün önce"


def _alindi(n: int) -> str:
    """When a cached set was fetched. _gun() already carries its own "önce",
    so reusing it here produced "bugün önce alındı"."""
    return ("bugün alındı" if n == 0 else
            "dün alındı" if n == 1 else f"{n} gün önce alındı")


def _pill(state: str) -> str:
    return (f'<span class="pill s-{state}"><span class="dot"></span>'
            f'{STATE_LABEL[state]}</span>')


AP_STATE = {"acik": ("flying", "Trafik var"),
            "tarifeli": ("scheduled", "Tarifeli, gözlenemiyor"),
            "durgun": ("stopped", "Durgun")}


def _airport_rows(data: dict) -> str:
    out = []
    for a in data["airport_view"]:
        cls, label = AP_STATE[a["state"]]
        row_cls = ' class="r-stopped"' if a["state"] == "durgun" else ""
        out.append(
            f'<tr{row_cls}>'
            f'<td><span class="code">{_e(a["iata"])}</span> '
            f'<span class="name">{_e(a["city"])}</span>'
            f'<div class="meta">{_e(a["icao"])} {_e(a["country"] or "")}</div></td>'
            f'<td><span class="pill s-{cls}"><span class="dot"></span>{label}</span></td>'
            # Every number says its own unit. Four numeric columns ran side by
            # side here -- 1611, 13, 2717, 12 -- where two count flights and
            # two count airlines, and the only thing distinguishing them was a
            # header row scrolled off the top.
            f'<td class="num">{a["legs"] or "—"}'
            + (f'<div class="meta">sefer</div>' if a["legs"] else "")
            + '</td>'
            f'<td class="num">{a["carriers_seen"] or "—"}'
            + (f'<div class="meta">havayolu</div>' if a["carriers_seen"] else "")
            + '</td>'
            f'<td class="num">{a["sched_weekly"] or "—"}'
            + ('<div class="meta">sefer/hafta</div>' if a["sched_weekly"] else "")
            + (f'<div class="meta">{a["carriers_sched"]} havayolu</div>'
               if a["carriers_sched"] else "")
            + '</td>'
            f'<td>{_who(a, data["carriers_cfg"])}</td>'
            '</tr>')
    return "".join(out)


def _who(a: dict, cfg: dict) -> str:
    """The carriers serving this airport, named rather than counted.

    On a scheduled-but-unobservable airport this is the whole answer the row
    has: ADS-B sees nothing, so the timetable is the only thing that can say
    who is still flying there. Marked as such, because a timetable is an
    intention and observed traffic is a fact.
    """
    if not a["who"]:
        return '<span class="meta">—</span>'
    chips = "".join(
        f'<span class="who-chip" title="{_e(cfg.get(c, {}).get("name", c))} — '
        f'{"gözlenen sefer" if a["legs"] else "tarifede haftalık"}: {round(n)}">'
        f'{_e(c)}</span>'
        for c, n in a["who"])
    tag = ("" if a["legs"] else
           '<div class="meta">tarifeye göre — uçuş verisi yok</div>')
    return f'<div class="who-list">{chips}</div>{tag}'


BOARD_VERDICT = {
    "ok": ("flying", "Tahta okundu"),
    "thin": ("partial", "Tahta çöktü — veri şüpheli"),
    "empty": ("stopped", "Tahta boş döndü — KAYNAK ARIZASI"),
    "unproven": ("unknown", "Geçmiş yetersiz"),
}


def _boards_section(data: dict) -> str:
    """What the published boards say for the airports we cannot observe.

    The verdict column is the point. This endpoint is undocumented and the
    realistic way it fails is by returning an empty list rather than an error,
    which would otherwise render as every carrier having stopped overnight.
    An `empty` or `thin` verdict therefore reads as a fault in the source and
    says so in those words, rather than as a finding about traffic.
    """
    boards = data.get("boards") or []
    if not boards:
        return ""
    ap = data["airports"]
    # Before MIN_HISTORY_DAYS there is no median for any airport, so the column
    # was seven rows of "—" next to seven "Geçmiş yetersiz" pills saying the
    # same thing twice. Draw it once it holds a number.
    show_median = any(b["median"] is not None for b in boards)
    rows = []
    for b in boards:
        cls, label = BOARD_VERDICT.get(b["verdict"], ("unknown", b["verdict"]))
        cfg = ap.get(b["airport"], {})
        broken = b["verdict"] in ("empty", "thin")
        chips = "".join(f'<span class="who-chip">{_e(c)}</span>'
                        for c in b["carriers"][:14])
        row_cls = ' class="r-stopped"' if broken else ""
        median = ("" if not show_median else
                  # No unit label here: it sits directly beside a cell in the
                  # same unit, under a header that names it.
                  f'<td class="num">{"—" if b["median"] is None else round(b["median"])}</td>')
        # A flagged board's carrier list is withheld rather than shown short:
        # a half-fetched list read as "these are the ones still flying" is
        # exactly the inference the verdict exists to block.
        who = (f'<div class="who-list">{chips}</div>'
               if chips and not broken else '<span class="meta">—</span>')
        rows.append(
            f'<tr{row_cls}>'
            f'<td><span class="code">{_e(cfg.get("iata", b["airport"]))}</span> '
            f'<span class="name">{_e(cfg.get("city", ""))}</span>'
            f'<div class="meta">{_e(b["airport"])}</div></td>'
            f'<td><span class="pill s-{cls}"><span class="dot"></span>{label}</span></td>'
            f'<td class="num">{b["flights"]}<div class="meta">kayıt</div></td>'
            f'{median}'
            f'<td>{who}</td>'
            '</tr>')
    if show_median:
        medyan_th = ('<th class="num">Medyan'
                     '<div class="meta">28 gün</div></th>')
        medyan_notu = (
            '<b>Medyan</b> sütunu önceki günlerin ortancası; bugünkü sayı onun '
            'çok altına düşerse kaynağın arızalandığı varsayılır, '
            'havayollarının uçmayı bıraktığı değil.')
    else:
        medyan_th = ""
        medyan_notu = (
            f'Bugünkü sayının çok altına düştüğü zaman kaynak arızası sayılacağı '
            f'<b>medyan</b> sütunu henüz yok: karşılaştırma '
            f'{flightboard.MIN_HISTORY_DAYS} günlük geçmiş biriktiğinde açılır, '
            f'o güne kadar buradaki sayılar yalnızca ham kayıt adedidir ve '
            f'hiçbir yargıya dayanak değildir.')

    flagged = [b for b in boards if b["verdict"] in ("empty", "thin")]
    warn = ""
    if flagged:
        names = ", ".join(_e(ap.get(b["airport"], {}).get("city", b["airport"]))
                          for b in flagged)
        warn = (f'<div class="notice-inline"><b>Dikkat: {names}</b> için tahta '
                f'beklenenin çok altında veri döndürdü. Bu <i>trafik yok</i> '
                f'demek değildir — büyük ihtimalle kaynak arızası. O '
                f'havalimanları bugünkü okumadan çıkarılmıştır.</div>')
    return f"""
<section>
  <h2>Kör havalimanları — varış/kalkış tahtası</h2>
  <p class="sub prose">ADS-B'nin göremediği yedi havalimanı için yayımlanmış
  tahta (FlightStats). Bu bir <b>liste</b>dir, transponder görüntüsü değil —
  o yüzden ayrı tabloda tutulur ve uçuş verisiyle asla toplanmaz.
  {medyan_notu}</p>
  {warn}
  <div class="tablewrap"><table>
    <thead><tr><th>Havalimanı</th><th>Durum</th>
      <th class="num">Bugün<div class="meta">tahta kaydı</div></th>
      {medyan_th}<th>Tahtada görünen havayolları</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</section>
"""


def _blind_news_section(data: dict) -> str:
    """Press coverage for the airports observation cannot reach.

    Rendered as links and dates, never as a verdict: this is the only part of
    the report where nothing can be checked against a flight, so it has to be
    obvious that a human is being asked to read rather than told an answer.
    """
    all_blocks = data.get("blind_news") or []
    blocks = [b for b in all_blocks if b["items"]]
    failed = [b for b in all_blocks if b.get("failed")]
    if not blocks and not failed:
        return ""
    cards = []
    for b in blocks:
        items = "".join(
            f'<li><a href="{_e(it["url"])}" target="_blank" rel="noopener">'
            f'{_e(it["title"])}</a>'
            f'<span class="meta">{_gun(it["age_days"])}'
            + (f' · <span class="tag s-{it["signal"]}">'
               f'{_e(SIGNAL_LABEL[it["signal"]])}</span>'
               if it["signal"] != "mentioned" else "")
            + '</span></li>'
            for it in b["items"])
        cards.append(
            f'<div class="card"><div class="hd"><b>{_e(b["iata"])}</b> '
            f'<span class="name">{_e(b["city"])}</span></div>'
            f'<ul class="blind-news">{items}</ul></div>')

    # A dead source is reported, not hidden. Dropping the section left the
    # reader unable to tell "basında bir şey yok" from "basına sorulamadı",
    # and those two mean opposite things for an airport nothing else can see.
    uyari = ""
    if failed:
        yerler = ", ".join(f'{_e(b["iata"])} ({_e(b["city"])})' for b in failed)
        uyari = (f'<div class="notice"><span class="t">Kaynak yanıt vermedi</span>'
                 f'<p>Bu çalıştırmada {len(failed)} havalimanı için basın '
                 f'sorgusu HTTP hatası döndürdü: {yerler}. Buralar hakkında '
                 f'<i>haber çıkmadığı</i> anlamına gelmez — <i>sorulamadığı</i> '
                 f'anlamına gelir. Arşivde kaydı olanlar aşağıda, alındıkları '
                 f'tarihle birlikte; bugün doğrulanmadılar.</p></div>')

    # Cached sets for the airports that could not be asked. Marked by age and
    # kept visually apart from a live answer, because for these seven nothing
    # else can corroborate them.
    for b in failed:
        if not b.get("stale"):
            continue
        items = "".join(
            f'<li><a href="{_e(it["url"])}" target="_blank" rel="noopener">'
            f'{_e(it["title"])}</a>'
            f'<span class="meta">{_alindi(b["stale"]["age_days"])}</span>'
            f'</li>' for it in b["stale"]["items"])
        cards.append(
            f'<div class="card stale"><div class="hd"><b>{_e(b["iata"])}</b> '
            f'<span class="name">{_e(b["city"])}</span>'
            f'<span class="tag s-mentioned">arşiv</span></div>'
            f'<ul class="blind-news">{items}</ul></div>')
    return f"""
<section>
  <h2>Kör havalimanları — basından</h2>
  <p class="sub prose">Bu yedi havalimanında ADS-B alıcısı yok: uçuş verisi
  <b>hiç</b> gelmiyor, dolayısıyla "durdu mu" sorusu gözlemle
  cevaplanamıyor. Tarife ne <i>planlandığını</i> söyler, basın ne
  <i>olduğunu</i>. Aşağısı karar değil, okumanız için bağlantı — havayolu
  adıyla değil havalimanı adıyla arandı, çünkü tek bir manşet çoğu zaman
  birkaç havayolunu birden adlandırıyor.</p>
  {uyari}
  <div class="cards">{"".join(cards)}</div>
</section>
"""


def _ratio_ready(data: dict) -> bool:
    """Does the observed/scheduled column have anything to say yet?

    Below MIN_RATIO_DAYS it had a cell for every carrier and a number for none
    of them: twenty rows of "0 / 819 — oran için veri yetersiz", where the zero
    is not a finding but a coverage gap. A column that cannot be read is worse
    than a missing one, so it is left out entirely until a percentage can be
    published, and the section says so in one line instead of twenty.
    """
    return any((c.get("ratio") or {}).get("ratio") is not None
               for c in data["carriers"])


def _rows(data: dict) -> str:
    ap = data["airports"]
    show_ratio = _ratio_ready(data)
    out = []
    order = {"stopped": 0, "partial": 1, "unknown": 2, "scheduled": 3, "flying": 4}
    for c in sorted(data["carriers"], key=lambda r: (order[r["state"]], -r["legs"])):
        at = " ".join(f'{ap[i]["iata"]}·{n}' for i, n in
                      sorted(c["seen_at"].items(), key=lambda kv: -kv[1])) or "—"
        heads = "".join(
            f'<div class="head"><span class="tag s-{h["signal"]}">'
            f'{_e(SIGNAL_LABEL[h["signal"]])}</span>'
            f'<a href="{_e(h["url"])}" target="_blank" rel="noopener">{_e(h["title"])}</a>'
            f'<span class="meta"> · {_gun(h["age_days"])}</span></div>'
            for h in c["news"])
        note = c.get("note")
        if note:
            kaynaklar = " ".join(
                f'<a href="{_e(u)}" target="_blank" rel="noopener">[{i + 1}]</a>'
                for i, u in enumerate(note["sources"][:4]))
            heads += (f'<div class="head"><span class="tag s-mentioned">web</span>'
                      f'{_e(note["note"])} {kaynaklar}</div>')
        stale = c.get("stale")
        if stale:
            heads += "".join(
                f'<div class="head"><span class="tag s-mentioned">eski</span>'
                f'<a href="{_e(h["url"])}" target="_blank" rel="noopener">'
                f'{_e(h["title"])}</a>'
                f'<span class="meta"> · {_alindi(stale["age_days"])},'
                f' bugün doğrulanamadı</span></div>'
                for h in stale["items"])
        # "basında ilgili haber yok" is a claim about the press. Only make it
        # when the press was actually reachable.
        empty = ('kaynak yanıt vermedi, arşivde de kayıt yok'
                 if c.get("news_failed") else 'basında ilgili haber yok')
        heads = f'<div class="heads">{heads}</div>' if heads else (
            f'<div class="heads"><div class="head meta">{empty}</div></div>')
        ratio_cell = ""
        if show_ratio:
            rt = c.get("ratio") or {}
            sched_cell = "—"
            if rt.get("ratio") is not None:
                pct = int(rt["ratio"] * 100)
                cls = ("flying" if pct >= 80 else "partial" if pct >= 30 else "stopped")
                sched_cell = (f'<b class="s-{cls}">%{pct}</b>'
                              f'<div class="meta">haftada {rt["observed"]:.0f} / '
                              f'{rt["scheduled"]} sefer/hafta, '
                              f'{rt["pairs"]}/{rt["pairs_total"]} çiftte — '
                              f'tarifenin %{int(rt["share"] * 100)}\u2019i</div>')
            elif rt.get("scheduled"):
                sched_cell = (f'<span class="meta">tarifede haftada '
                              f'{rt["scheduled"]}; gözlem oranı için gün sayısı '
                              f'yetersiz</span>')
            ratio_cell = f'<td class="num">{sched_cell}</td>'
        cls = f' class="r-{c["state"]}"' if c["state"] in ("stopped", "partial") else ""
        out.append(
            f'<tr{cls}>'
            f'<td><div class="code">{_e(c["code"])}</div>'
            f'<div class="meta">{_e(c["iata"] or "")} {_e(c["country"] or "")}</div></td>'
            f'<td><div class="name">{_e(c["name"])}</div>{heads}</td>'
            f'<td>{_pill(c["state"])}<div class="why">{_e(c["why"])}</div></td>'
            f'<td class="num">{c["legs"] or "—"}'
            + ('<div class="meta">sefer</div>' if c["legs"] else "")
            + '</td>'
            f'<td class="at">{_e(at)}</td>'
            f'{ratio_cell}'
            f'</tr>')
    return "".join(out)


def render(data: dict) -> str:
    cov = data["coverage"]
    cs = data["carriers"]
    n = lambda s: sum(1 for c in cs if c["state"] == s)  # noqa: E731
    gen = data["generated_at"].strftime("%d.%m.%Y %H:%M UTC")
    d1, d2 = data["window"]

    d = data["delta"]
    # Say which comparison was actually made. Silence here reads as "nothing
    # moved", and on a blind day the airport columns were not compared at all.
    kapali = ("" if d.get("airports_compared") else
              ' Bugün kapsama testi geçilmediği için <b>havalimanı '
              'kayıp/kazanç satırları üretilmedi</b> — boş bir pencereden '
              'düşen havalimanı, uçmayı bırakmış havayolu demek değildir. '
              'Aşağıdakiler yalnızca durum değişiklikleridir.')
    if not d["since"]:
        delta_html = ""
    elif not d["changes"]:
        delta_html = (f'<section><h2>Son rapordan beri</h2>'
                      f'<p class="sub prose">{_e(d["since"])} tarihli rapora göre '
                      f'durum değişikliği yok.{kapali}</p></section>')
    else:
        satir = "".join(
            f'<tr><td><span class="code">{_e(c["carrier"])}</span> '
            f'<span class="name">{_e(c["name"])}</span></td>'
            f'<td>{_e(c["kind"])}</td>'
            f'<td class="at">{_e(c["was"] or "—")}</td>'
            f'<td class="at">{_e(c["now"] or "—")}</td></tr>'
            for c in d["changes"])
        delta_html = (
            f'<section><h2>Son rapordan beri</h2>'
            f'<p class="sub prose">{_e(d["since"])} tarihli rapora göre değişenler. '
            f'Anlık durum tablosu aşağıda; burası sadece <b>hareket edenler</b>.'
            f'{kapali}</p>'
            f'<div class="scroll"><table><thead><tr><th>Havayolu</th><th>Ne oldu</th>'
            f'<th>Önce</th><th>Sonra</th></tr></thead><tbody>{satir}</tbody></table></div>'
            f'</section>')

    warn = []
    if not data["baseline_ready"]:
        warn.append(
            "<b>Referans dönem henüz yok.</b> <code>backfill-baseline</code> "
            "tamamlanana kadar bu rapor bir havayolunun <i>uçup uçmadığını</i> "
            "söyleyebilir, ama <i>eskisinden az uçup uçmadığını</i> söyleyemez. "
            "Çatışma öncesi dönemin verisi indiğinde frekans karşılaştırması "
            "(“haftada 12 sefer, normalde 21”) bu tabloya eklenecek.")
    if not data["with_news"]:
        warn.append("<b>Haber taraması atlandı.</b> Bu çalıştırmada ADS-B "
                    "kapsamı dışındaki havayolları için ikinci kaynak yok.")
    elif data.get("news_failures"):
        # Atlanmakla yanıtsız kalmak aynı şey değil, ve ikisi de "haber yok"
        # değil. Kaynak sustuğunda rapor bunu söylemek zorunda.
        warn.append(
            f'<b>Basın kaynağı {data["news_failures"]}/{data["news_asked"]} '
            f'havayolu sorgusunda yanıt vermedi.</b> Google News RSS HTTP '
            f'hatası döndürdü; o satırlarda "hat kesintisi bildirilmedi" ifadesi '
            f'<i>sorulup bulunamadığı</i> değil, <i>sorulamadığı</i> anlamına '
            f'gelir. Aynı sorgular başka bir ağdan çalışıyor, yani bu '
            f'çalıştırmaya özgü geçici bir arıza.')

    # The observed/scheduled column carries two numbers that look like the
    # Sefer column beside it and are not: that one is every leg counted at
    # every monitored airport over the window, this one is scaled to a week and
    # restricted to the city pairs both sources cover. Emirates read 332 and 35
    # side by side with nothing saying why.
    if _ratio_ready(data):
        oran_th = ('<th class="num">Gözlenen / tarifeli'
                   '<div class="meta">Sefer sütunuyla aynı şey değil: haftaya '
                   'ölçeklenmiş, ve yalnızca ölçülebilen şehir '
                   'çiftleri</div></th>')
        oran_aciklama = (
            '<p class="sub prose"><b>Gözlenen / tarifeli</b> sütunu asıl cevabı '
            'verir: havayolunun izlenen havalimanları arasında gerçekten uçtuğu '
            'haftalık sefer sayısı, tarifesinde planladığı sayıya bölünmüş. '
            "%100'e yakınsa tarifesini uyguluyor; düşükse fiilen kesmiş "
            'demektir. İki taraf da <b>aynı şehir çiftlerini</b> sayar, yoksa '
            'uzun menzilli havayolları haksız yere düşük görünür — bu yüzden '
            'soldaki <b>Sefer</b> sütunundan küçüktür, o sütun bütün '
            'havalimanlarındaki bütün seferleri sayar.</p>')
    else:
        oran_th = ""
        oran_aciklama = (
            f'<p class="sub prose"><b>Gözlenen / tarifeli</b> sütunu bu raporda '
            f'yok. Havayolunun uçtuğu haftalık seferi tarifesine bölen o oran '
            f'iki şart ister: bir şehir çiftinin <b>kendi</b> verisini getiren '
            f'sorgunun en az {MIN_RATIO_DAYS} gün çalışmış olması, ve bu şekilde '
            f'ölçülebilen çiftlerin havayolunun tarifesinin en az '
            f'%{int(config.MIN_COMPARABLE_SHARE * 100)}’ini kapsaması. Bir '
            f'kalkış yalnızca kalkış havalimanının sorgusuyla görünür; o sorgu '
            f'zayıf döndüğünde sefer eksilir ama tarife eksilmez, ve aradaki '
            f'fark havayolunun kesintisi gibi okunur. Şu an ölçülebilen çiftler '
            f'tarifenin küçük bir kısmını kapsıyor, o yüzden sütun boş '
            f'hücrelerle çizilmek yerine hiç çizilmiyor.</p>')

    fircards = "".join(
        f'<div class="card"><div class="hd"><span class="fir">{_e(f["fir"])}</span>'
        f'<span class="cnt">{f["total_transits"]}</span></div>'
        f'<div class="place">{_e(f["name"])}'
        + (' <span class="watch">CZIB</span>' if f.get("czib_watch") else "")
        + '</div><div class="who">'
        + (" ".join(_e(c["carrier"]) for c in f["carriers"]) or "—")
        + "</div></div>"
        for f in sorted(data["firs"], key=lambda x: -x["total_transits"])
        if f["total_transits"] or f.get("czib_watch"))

    adv = "".join(
        f'<div class="head"><span class="tag s-partial">{_e(a["ref"])}</span>'
        f'<a href="{_e(a["url"])}" target="_blank" rel="noopener">'
        f'{_e(a["title"] or a["ref"])}</a>'
        f' <span class="meta">yayım {_e(a["revision"])} · geçerlilik {_e(a["valid_to"])}</span>'
        + (f'<div class="why" style="max-width:70ch">{_e(a["summary"])}</div>'
           if a.get("summary") else "")
        + "</div>" for a in data["advisories"]) or '<p class="sub">Kayıtlı bülten yok.</p>'

    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GulfWatch — Ortadoğu havayolu operasyon raporu {_e(data['as_of_day'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600&display=swap" rel="stylesheet">
<style>{CSS}</style>
<div class="wrap">
<header>
  <h1 class="brand">Gulf<span>Watch</span></h1>
  <div class="deck">Ortadoğu havayolu operasyonları</div>
  <div class="eyebrow">Elle çalıştırılan rapor
    <a class="sibling" href="dashboard.html">Pano ve JSON API →</a></div>
  <p class="sub prose">İzlenen Körfez ve Levant havalimanlarında hangi
  havayolunun hâlâ uçtuğu. Dört kaynak sırayla okunuyor:
  <b>gözlemlenen uçuş verisi</b>, <b>yayımlanmış tarifeler</b>,
  <b>basın</b>, ve hiçbiri konuşmuyorsa <b>web araması</b>.
  Veriler <b>{_e(data['as_of_day'])}</b>
  gününe ait; gözlem penceresi {_e(d1)} → {_e(d2)} ({data['days']} gün).
  Basın taraması son {data["news_days"]} günle sınırlı.
  Rapor {_e(gen)} tarihinde üretildi.</p>
</header>

<section>
  <h2>Tek bakışta</h2>
  <div class="band">
    <div class="stat"><span class="k">Havayolu</span><span class="v">{len(cs)}</span>
      <span class="n">izlenen toplam</span></div>
    <div class="stat"><span class="k">Uçuyor</span>
      <span class="v s-flying">{n('flying')}</span><span class="n">havada görüldü, hat kesintisi bildirilmedi</span></div>
    <div class="stat"><span class="k">Kısmen kesti</span>
      <span class="v s-partial">{n('partial')}</span><span class="n">uçuyor ama hat kesmiş</span></div>
    <div class="stat"><span class="k">Durdurdu</span>
      <span class="v s-stopped">{n('stopped')}</span><span class="n">görülmedi + basın doğruluyor</span></div>
    <div class="stat"><span class="k">Tarifede var</span>
      <span class="v s-scheduled">{n('scheduled')}</span><span class="n">kapsama dışı, tarifesi duruyor</span></div>
    <div class="stat"><span class="k">Bilinmiyor</span>
      <span class="v s-unknown">{n('unknown')}</span><span class="n">iki kaynak da sessiz</span></div>
  </div>
  <p class="cov-line">Yukarıdaki altı sayı <b>havayolu sayısı</b> ve toplamı
  {len(cs)} eder. Aşağıdaki satır ise <b>bu verinin ne kadar sağlam olduğunu</b>
  söyler — aynı cetvelin sayıları değil, o yüzden ayrı duruyor.</p>
  <div class="cov">
    <span class="pill s-{"flying" if cov["verdict"] == "ok" else "partial"}"><span class="dot"></span>{
      _e(COV_LABEL.get(cov["verdict"], cov["verdict"]))}</span>
    <p>{data["as_of_day"]} günü kontrol havayollarından <b>{cov['control_flights']}
    sefer</b> görüldü; bu, son 28 günün ortancasının <b>{cov['score']} katı</b>.
    Gözlem penceresindeki {data["days"]} günün <b>{data["observed_days"]}
    tanesine</b> bakılabildi{
      f", ve oran sütununun yayımlanması için gereken {MIN_RATIO_DAYS} güne "
      f"{MIN_RATIO_DAYS - data['observed_days']} gün kaldı"
      if data["observed_days"] < MIN_RATIO_DAYS else ""}. Haftalık sayılar
    yalnızca bu günlere göre ölçekleniyor.</p>
  </div>
  {"".join(f'<div class="notice"><span class="t">Önce bunu okuyun</span><p>{w}</p></div>' for w in warn)}
</section>

{delta_html}
<section>
  <h2>Bu rapor nasıl okunur</h2>
  <p class="sub prose">Soru şu: <i>bu havayolu Ortadoğu uçuşlarını kesti mi?</i>
  Tek bir kaynak bunu güvenilir cevaplayamıyor, o yüzden iki kaynak
  birbirine karşı okunuyor.</p>
  <div class="cards">
    <div class="card"><div class="hd"><span class="fir">Uçuş verisi</span></div>
      <div class="place">Uçakların yayınladığı ADS-B sinyalleri, gönüllü
      alıcılardan toplanıyor. <b>Ne uçtuğunu</b> gösterir — ama yalnızca alıcı
      bulunan yerlerde. BAE, Katar, Bahreyn ve Ürdün'de kapsama güçlü;
      <b>Suudi Arabistan, Kuveyt, Irak ve İran'da neredeyse hiç yok.</b></div></div>
    <div class="card"><div class="hd"><span class="fir">Tarife</span></div>
      <div class="place">Havayollarının yayımladığı uçuş tarifeleri (AirLabs).
      <b>Ne uçurmayı planladığını</b> gösterir — alıcıdan bağımsız, yani
      <b>ADS-B'nin kör olduğu Kuveyt, Suudi Arabistan, Irak ve İran'da da.</b>
      Tarifede sefer durması uçuşun gerçekleştiği anlamına gelmez; günlük
      iptaller tarifeye yansımaz.</div></div>
    <div class="card"><div class="hd"><span class="fir">Basın</span></div>
      <div class="place">Google News üzerinden havayolunun adının geçtiği,
      son {data["news_days"]} güne ait başlıklar. <b>Ne duyurulduğunu</b>
      gösterir — her yerde, ama yalnızca hakkında yazılacak kadar büyük
      havayolları için. Başlıkları bir dil modeli okuyor: "X tarifesini
      koruyor, Y iptal ediyor" gibi cümlelerde eylemi doğru havayoluna
      bağlayabilmek için. Model bir başlığı bu bölgeyle ilgisiz bulursa
      <b>“bu bölgeyle ilgisiz”</b> etiketiyle görünür ve <b>durumu
      değiştirmez</b>; bir havayolunu ancak izlediğimiz bir havalimanını ya da
      bölgeyi adlandıran başlık “kesti” sayabilir — yoksa başka bir kıtadaki
      hat açılışı burada hat kesintisi diye okunur. Duyuru gözlem değildir.
      Kaynak yanıt vermezse rapor
      bunu en üstte söyler; sessizce “haber yok”a çevirmez. O durumda en son
      cevap alınan başlıklar <b>“arşiv”</b> etiketiyle ve alındıkları tarihle
      gösterilir — okumanız için, çünkü <b>durumu değiştirmezler:</b> bugün
      doğrulanamayan bir başlık bugünün kararını veremez.</div></div>
    <div class="card"><div class="hd"><span class="fir">Web araması</span></div>
      <div class="place"><b>Son çare.</b> Yalnızca ilk üç kaynağın da sessiz
      kaldığı havayolları için çalışır ve <b>durumu değiştirmez</b> — sadece
      kaynaklı bir not ekler. En zayıf kaynak: model enum'dan seçmek yerine
      düz metin yazar, yani yanılabilir. Yanındaki numaralar kaynak
      bağlantılarıdır, kontrol için oradalar.</div></div>
  </div>
  <p class="sub prose">Sayfadaki sayılar <b>dört ayrı cetvelden</b> geliyor ve
  hiçbiri diğeriyle toplanamaz. Bir tabloda bir sayı görürseniz altındaki küçük
  etiket hangi cetvel olduğunu söyler:</p>
  <div class="glossary">
    <div><span class="u">sefer</span><p>Uçuş verisinde <b>görülmüş</b> tek bir
      iniş ya da kalkış. Olan şey.</p></div>
    <div><span class="u">sefer/hafta</span><p>Havayolunun <b>tarifesinde</b> duran
      haftalık sefer sayısı. Planlanan şey — uçtuğu anlamına gelmez.</p></div>
    <div><span class="u">kayıt</span><p>Yayımlanmış varış/kalkış
      <b>tahtasındaki bir satır</b>. Liste kaydıdır, transponder görüntüsü
      değil; seferle asla toplanmaz.</p></div>
    <div><span class="u">kat</span><p>Kapsama puanı: bugün kontrol
      havayollarından görülen trafiğin, son 28 günün ortancasına <b>oranı</b>.
      Sayı değil, çarpan.</p></div>
  </div>
  <p class="sub prose"><b>“Kesinti” bu sayfada tek bir şey demek:</b> havayolunun
  bir hattı kesmesi. Alıcılarımızın kör kaldığı gün için bu kelime hiç
  kullanılmıyor — o gün “bakılamadı” diye geçer. İkisini aynı kelimeyle yazmak,
  raporun bütün varlık sebebini ortadan kaldırırdı.</p>
  <p class="sub prose">Bir çelişki olduğunda <b>gözlem duyuruyu yener.</b> Uçarken
  görülen bir havayolu, basın “kesti” dese bile “Durdurdu” sayılmaz; “Kısmen
  kesti” olur. Durumların anlamı:</p>
  <div class="scroll"><table>
    <thead><tr><th>Durum</th><th>Ne demek</th><th>Nasıl karar verildi</th></tr></thead>
    <tbody>
      <tr><td>{_pill('flying')}</td>
        <td>Havayolu izlenen havalimanlarında uçarken görüldü ve hat kesintisi
        bildirimi yok.</td><td class="why">ADS-B'de sefer var, basında hat kesintisi haberi yok</td></tr>
      <tr class="r-partial"><td>{_pill('partial')}</td>
        <td>Uçuyor, ama bazı hatlarını kestiği bildiriliyor. Çatışma
        döneminde en yaygın durum bu. <b>İki ayrı temeli var</b> ve satırın
        gerekçesi hangisi olduğunu söylüyor: ya uçarken görüldü, ya da
        görülmedi ama tarifesi duruyor. İkincisi kapsama dışındaki bir havayolu
        için tek okunabilir cevap — sayı sütunu boş kalır.</td>
        <td class="why">basında hat kesintisi haberi var +
        (ADS-B'de sefer var <i>ya da</i> tarifede sefer var)</td></tr>
      <tr><td>{_pill('scheduled')}</td>
        <td>ADS-B'de görülmedi ama tarifesinde sefer duruyor. Genellikle
        kapsama dışındaki bir havalimanına (Riyad, Cidde, Kuveyt, Bağdat…)
        uçuyor demektir.</td>
        <td class="why">ADS-B'de sefer yok + tarifede sefer var</td></tr>
      <tr class="r-stopped"><td>{_pill('stopped')}</td>
        <td>Ne görüldü, ne tarifesinde sefer var, üstelik basın durdurduğunu
        yazıyor. Üç kaynağın da aynı yönü gösterdiği tek durum.</td>
        <td class="why">üç kaynak da aynı yönde</td></tr>
      <tr><td>{_pill('unknown')}</td>
        <td>Üç kaynağın hiçbirinde izine rastlanmadı. <b>Bu “durdurdu” demek
        değildir</b> — izlediğimiz havalimanlarına hiç uçmuyor da olabilir.</td>
        <td class="why">üç kaynak da sessiz</td></tr>
    </tbody>
  </table></div>
  <div class="notice"><span class="t">En önemli uyarı</span>
    <p><b>Veri yokluğu, uçuş yokluğu değildir.</b> Suudi Arabistan, Kuveyt, Irak
    ve İran üzerinde ADS-B kapsaması çok zayıf; bir havayolu oralarda pekâlâ
    uçuyor olup burada hiç sefer göstermeyebilir. Bu yüzden görülmeyen her
    havayolu otomatik olarak “Bilinmiyor” sayılır, “Durdurdu” değil.</p>
    <p>Aynı kural <b>güne</b> de uygulanır. Her gün üç halden birindedir:
    <b>bakıldı ve veri sağlam</b>, <b>bakıldı ama alıcılar zayıftı</b>, ya da
    <b>hiç bakılmadı</b>. Yalnızca ilki sessizlik sayılır — bir havayolunun
    “kaç gündür görünmediği” yalnızca baktığımız günlerden hesaplanır, ve
    haftalık sefer sayıları yalnızca o günlere göre ölçeklenir. Baktığımız gün
    sayısı {MIN_RATIO_DAYS}’in altındaysa oran hiç yayımlanmaz.</p>
    <p>Bu üç hal <b>bütün ağ için</b> verilir, ve bir seferi görmeye tek başına
    yetmez. Her sefer tek bir sorguyla yakalanır: kalkış havalimanının kalkış
    sorgusu ya da varış havalimanının varış sorgusu. Bunlardan biri o gün zayıf
    döndüyse, gün ağ için “veri sağlam” olsa bile <b>o sefer için</b>
    bakılmamış sayılır. 19 Ağustos’ta Doha’nın kalkış sorgusu normalin
    %27–56’sını getirirken varış sorgusu %66’daydı: Doha–Atlanta “kesildi”
    diye işaretlendi, Atlanta–Doha ise aynı tabloda uçmaya devam ediyordu. O
    yüzden hem sessizlik hem de oran, seferin kendi sorgusunun çalıştığı
    günlerden hesaplanır. Bu ayrımın olmadığı bir sürüm, verinin bittiği yeri
    uçuşların bittiği yer sanmıştı.</p></div>
</section>

<section>
  <h2>Havalimanları</h2>
  <p class="sub prose">Sorunun diğer yarısı: <i>bu havalimanına hâlâ uçuluyor
  mu?</i> Önce sorunlular. <b>Tarifeli</b> olanlar ADS-B kapsamımızın dışında —
  uçuş verisi göremiyoruz ama havayolları tarifelerinde sefer tutuyor.</p>
  <div class="scroll"><table>
    <thead><tr><th>Havalimanı</th><th>Durum</th>
      <th class="num">Gözlenen sefer<div class="meta">uçuş verisi</div></th>
      <th class="num">Görülen havayolu<div class="meta">uçuş verisi</div></th>
      <th class="num">Tarifede sefer<div class="meta">tarife</div></th>
      <th>Kimler uçuyor</th></tr></thead>
    <tbody>{_airport_rows(data)}</tbody>
  </table></div>
</section>

{_boards_section(data)}
{_blind_news_section(data)}

<section>
  <h2>Havayolları</h2>
  <p class="sub prose">Önce dikkat gerektirenler: durduranlar, sonra kısmen
  kesenler, sonra hakkında bilgi olmayanlar. <b>Sefer</b> sütunu gözlem
  penceresinde izlenen havalimanlarında sayılan iniş/kalkış sayısı;
  <b>Nerede</b> sütunu bunun havalimanlarına dağılımı.</p>
  {oran_aciklama}
  <div class="scroll"><table>
    <thead><tr><th>Kod</th><th>Havayolu ve basında çıkanlar</th><th>Durum</th>
      <th class="num">Sefer<div class="meta">uçuş verisi</div></th>
      <th>Nerede görüldü<div class="meta">havalimanı·sefer</div></th>
      {oran_th}</tr></thead>
    <tbody>{_rows(data)}</tbody>
  </table></div>
</section>

<section>
  <h2>Hava sahası geçişleri · son 7 gün</h2>
  <p class="sub prose">Her uçuş bilgi bölgesinin (FIR) içinde canlı ADS-B ile
  sayılan farklı uçak sayısı. Körfez'de sürekli GPS karıştırması olduğu için
  konum doğruluğu düşük sinyaller sayıma katılmadan eleniyor.
  <b>CZIB</b> etiketi, EASA'nın operatörlere “kaçının” dediği hava sahasını
  gösterir — oradan geçen trafik ayrıca dikkate değerdir.</p>
  <div class="cards">{fircards}</div>
</section>

<section>
  <h2>EASA çatışma bölgesi bültenleri</h2>
  <p class="sub prose">Avrupa Havacılık Emniyeti Ajansı'nın yürürlükteki
  uyarıları. Bir bültenin yeniden yayımlanması ya da geçerlilik süresinin
  uzatılması, başlı başına bir sinyaldir.</p>
  <div class="heads">{adv}</div>
</section>

<section>
  <h2>Kaynaklar</h2>
  <dl class="src">
    <dt>Uçuş verisi</dt><dd>OpenSky Network — ticari olmayan kullanım. Gönüllü
      alıcı ağı: BAE, Katar, Bahreyn ve Ürdün'de güçlü; Suudi Arabistan,
      Kuveyt, Irak ve İran'da zayıf ya da yok.</dd>
    <dt>Canlı ADS-B</dt><dd>adsb.lol ve airplanes.live, ODbL 1.0 lisansı.</dd>
    <dt>Tarife</dt><dd>AirLabs — ücretsiz katman, aylık 1.000 sorgu. Yanıt 50
      kayıtta kırpıldığı için şehir çifti bazında sorgulanır ve önbelleğe
      alınır. Yalnızca ADS-B'nin kör olduğu havalimanları için harcanır.</dd>
    <dt>Basın</dt><dd>Google News RSS. Başlıkta havayolunun adı geçen haberler
      süzülür; anahtar kelimeyle sınıflandırılır. Karar vermez, okumanız için
      bağlantıyı önünüze koyar. Kaynak veri merkezi IP'lerine aralıklı olarak
      HTTP hatası döndürüyor; cevap alınan son başlıklar saklanır ve kesinti
      gününde arşiv olarak, tarihiyle gösterilir.</dd>
    <dt>Web araması</dt><dd>Mistral web arama aracı. Yalnızca diğer üç kaynağın
      da sessiz kaldığı havayolları için, çalıştırma başına en fazla
      {MAX_WEB_SEARCHES} sorgu. Durumu değiştirmez, kaynaklı not ekler.</dd>
    <dt>Uyarılar</dt><dd>EASA Çatışma Bölgesi Bilgi Bültenleri (CZIB). Bülten
      metinleri bir dil modeliyle tek cümleye indiriliyor; özet değişmesi için
      bültenin kendisinin değişmesi gerekir.</dd>
  </dl>
</section>

<footer>
  <p>Veri yokluğu hiçbir zaman uçuş yokluğunun kanıtı değildir. Ortak sefer
  (codeshare) ve kiralık uçuşlar ADS-B'de görünmez: bir havayolunun sattığı
  ama başka bir şirketin uçurduğu sefer, uçuran şirkete yazılır.</p>
  <p>Bu bir kişisel araştırma aracıdır, operasyonel karar sistemi değildir.
  Gerçek uçuş planlaması için kaynaklar AIP, NOTAM sistemi ve operatörün kendi
  risk değerlendirmesidir.</p>
</footer>
</div>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    data = collect(args.days, with_news=not args.no_news,
                   use_llm=not args.no_llm)
    # The report is the front page: it is the one artefact that explains what
    # every number rests on. The JSON dashboard sits beside it at
    # dashboard.html for the at-a-glance read and the API index.
    path = Path(args.out or (config.PUBLIC_DIR / "index.html"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    LOG.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
