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
MIN_RATIO_DAYS = 5

# Words that mean the article is about the region rather than this carrier.
GENERIC = re.compile(r"\b(which airlines|airlines (?:have|suspend|resume|cancel)|"
                     r"list of|roundup|factbox)\b", re.I)


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
    covered = conn.execute(
        """SELECT COUNT(DISTINCT f.dep_date) n FROM flight f
           JOIN coverage c ON c.day = f.dep_date
           WHERE f.dep_date BETWEEN ? AND ? AND c.verdict = 'ok'""",
        (since, until)).fetchone()["n"]
    if not covered:
        return {}
    trustworthy = covered >= MIN_RATIO_DAYS

    obs: dict[str, int] = defaultdict(int)
    for r in conn.execute(
            """SELECT carrier, dep_icao, arr_icao, COUNT(*) n FROM flight
               WHERE is_freight = 0 AND dep_date BETWEEN ? AND ?
                 AND carrier IS NOT NULL AND dep_icao <> arr_icao
               GROUP BY carrier, dep_icao, arr_icao""", (since, until)):
        if r["dep_icao"] in iata and r["arr_icao"] in iata:
            obs[r["carrier"]] += r["n"]

    sch: dict[str, int] = defaultdict(int)
    monitored = set(iata.values())
    for r in conn.execute(
            "SELECT carrier, dep_iata, arr_iata, weekly FROM route_schedule"):
        if r["dep_iata"] in monitored and r["arr_iata"] in monitored:
            sch[r["carrier"]] += r["weekly"] or 0

    out = {}
    for code in set(obs) | set(sch):
        flown = round(obs[code] * 7 / covered, 1)
        planned = sch[code]
        out[code] = {"observed": flown, "scheduled": planned,
                     "ratio": (round(flown / planned, 2)
                               if planned and trustworthy else None),
                     "days_covered": covered, "trustworthy": trustworthy}
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


def carrier_news(name: str, limit: int = 2, max_age: int = NEWS_MAX_AGE_DAYS) -> list[dict]:
    """Recent headlines that name this carrier.

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


def blind_news(max_age: int = NEWS_MAX_AGE_DAYS, per_airport: int = 3) -> list[dict]:
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
        out.append({"iata": icao, "city": city, "items": hits[:per_airport]})
    return out


def verdict(seen_at: dict, sched: dict | None, news: list[dict]) -> tuple[str, str]:
    """(durum, gerekçe) — üç kaynaktan.

    Sıralama: gözlem > tarife > haber. Uçtuğu görülen bir havayolu, basın ne
    derse desin uçuyordur; görülmeyen ama tarifesi duran bir havayolu için
    'durdurdu' demek, kapsama boşluğunu kesintiye dönüştürmek olur.
    """
    kesinti = any(x["signal"] == "stopped" for x in news)
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
        return "flying", f"{n} sefer havada görüldü, kesinti bildirimi yok"

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
    if any(x["signal"] == "resumed" for x in news):
        return "unknown", ("görülmedi ve tarifesinde sefer yok, ama basın yeniden "
                           "başladığını yazıyor — çelişki")
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
                       "unclear": "mentioned"}[got["action"]]


def diff_since_last(conn, rows: list[dict]) -> dict:
    """What moved since the previous run, and record today for the next one.

    A snapshot answers "where do things stand". An operator watching a
    conflict needs the other question — what changed — and the history to
    answer it is already in the database.
    """
    today = metrics.reference_day().isoformat()
    prev_day = conn.execute(
        "SELECT MAX(day) d FROM report_state WHERE day < ?", (today,)).fetchone()["d"]

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
    return {"since": prev_day, "changes": changes}


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
    for code, cfg in sorted(carriers.items(), key=lambda kv: kv[1]["name"]):
        news = []
        if with_news:
            news = carrier_news(cfg["name"], max_age=news_days)
            if use_llm:
                enrich(conn, code, cfg["name"], news)
            time.sleep(1.2)          # be polite to Google News
            LOG.info("%s: %s headlines", code, len(news))
        seen_at = dict(act["seen"].get(code, {}))
        sched = sched_by_carrier.get(code)
        state, why = verdict(seen_at, sched, news)
        rows.append({"code": code, "name": cfg["name"],
                     "iata": cfg.get("iata"), "country": cfg.get("country"),
                     "seen_at": seen_at, "legs": sum(seen_at.values()),
                     "baseline": base.get(code), "news": news,
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

    delta = diff_since_last(conn, rows)

    return {
        "generated_at": datetime.now(tz=timezone.utc),
        "delta": delta,
        "as_of_day": ref.isoformat(),
        "window": (act["since"], act["until"]),
        "days": days,
        "coverage": metrics.score_coverage(conn, ref),
        "baseline_ready": bool(base),
        "carriers": rows,
        "airports": config.airports(),
        "carriers_cfg": carriers,
        "airport_view": airport_view(conn, days, carriers),
        "blind_news": blind_news(news_days) if with_news else [],
        "boards": flightboard.by_airport(conn),
        "firs": firwatch.summary(conn, days=7),
        "schedule_coverage": schedules.coverage(conn),
        "advisories": advisories.current(conn),
        "with_news": with_news,
        "news_days": news_days,
        "use_llm": use_llm,
    }


# --- Render ----------------------------------------------------------------

STATE_LABEL = {"flying": "Uçuyor", "partial": "Kısmen kesti",
               "scheduled": "Tarifede var", "stopped": "Durdurdu",
               "unknown": "Bilinmiyor"}

SIGNAL_LABEL = {"stopped": "kesinti", "resumed": "yeniden başladı",
                "unaffected": "etkilenmedi", "mentioned": "ilgili"}

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
            f'<td class="num">{a["legs"] or "—"}</td>'
            f'<td class="num">{a["carriers_seen"] or "—"}</td>'
            f'<td class="num">{a["sched_weekly"] or "—"}'
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
    rows = []
    for b in boards:
        cls, label = BOARD_VERDICT.get(b["verdict"], ("unknown", b["verdict"]))
        cfg = ap.get(b["airport"], {})
        broken = b["verdict"] in ("empty", "thin")
        chips = "".join(f'<span class="who-chip">{_e(c)}</span>'
                        for c in b["carriers"][:14])
        row_cls = ' class="r-stopped"' if broken else ""
        median = "—" if b["median"] is None else round(b["median"])
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
            f'<td class="num">{b["flights"]}</td>'
            f'<td class="num">{median}</td>'
            f'<td>{who}</td>'
            '</tr>')
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
  <b>Medyan</b> sütunu önceki günlerin ortancası; bugünkü sayı onun çok
  altına düşerse kaynağın arızalandığı varsayılır, havayollarının uçmayı
  bıraktığı değil. Geçmiş {flightboard.MIN_HISTORY_DAYS} günden azken hiçbir
  yargıya varılmaz.</p>
  {warn}
  <div class="tablewrap"><table>
    <thead><tr><th>Havalimanı</th><th>Durum</th><th class="num">Kayıt</th>
      <th class="num">Medyan</th><th>Tahtada görünen havayolları</th></tr></thead>
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
    blocks = [b for b in data.get("blind_news") or [] if b["items"]]
    if not blocks:
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
    return f"""
<section>
  <h2>Kör havalimanları — basından</h2>
  <p class="sub prose">Bu yedi havalimanında ADS-B alıcısı yok: uçuş verisi
  <b>hiç</b> gelmiyor, dolayısıyla "durdu mu" sorusu gözlemle
  cevaplanamıyor. Tarife ne <i>planlandığını</i> söyler, basın ne
  <i>olduğunu</i>. Aşağısı karar değil, okumanız için bağlantı — havayolu
  adıyla değil havalimanı adıyla arandı, çünkü tek bir manşet çoğu zaman
  birkaç havayolunu birden adlandırıyor.</p>
  <div class="cards">{"".join(cards)}</div>
</section>
"""


def _rows(data: dict) -> str:
    ap = data["airports"]
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
        heads = f'<div class="heads">{heads}</div>' if heads else (
            '<div class="heads"><div class="head meta">basında ilgili haber yok</div></div>')
        rt = c.get("ratio") or {}
        sched_cell = "—"
        if rt.get("scheduled"):
            if rt.get("ratio") is not None:
                pct = int(rt["ratio"] * 100)
                cls = ("flying" if pct >= 80 else "partial" if pct >= 30 else "stopped")
                sched_cell = (f'<b class="s-{cls}">%{pct}</b>'
                              f'<div class="meta">{rt["observed"]:.0f} / {rt["scheduled"]}'
                              f' hafta</div>')
            else:
                sched_cell = (f'{rt["observed"]:.0f} / <b>{rt["scheduled"]}</b>'
                              f'<div class="meta">oran için veri yetersiz</div>')
        cls = f' class="r-{c["state"]}"' if c["state"] in ("stopped", "partial") else ""
        out.append(
            f'<tr{cls}>'
            f'<td><div class="code">{_e(c["code"])}</div>'
            f'<div class="meta">{_e(c["iata"] or "")} {_e(c["country"] or "")}</div></td>'
            f'<td><div class="name">{_e(c["name"])}</div>{heads}</td>'
            f'<td>{_pill(c["state"])}<div class="why">{_e(c["why"])}</div></td>'
            f'<td class="num">{c["legs"] or "—"}</td>'
            f'<td class="at">{_e(at)}</td>'
            f'<td class="num">{sched_cell}</td>'
            f'</tr>')
    return "".join(out)


def render(data: dict) -> str:
    cov = data["coverage"]
    cs = data["carriers"]
    n = lambda s: sum(1 for c in cs if c["state"] == s)  # noqa: E731
    gen = data["generated_at"].strftime("%d.%m.%Y %H:%M UTC")
    d1, d2 = data["window"]

    d = data["delta"]
    if not d["since"]:
        delta_html = ""
    elif not d["changes"]:
        delta_html = (f'<section><h2>Son rapordan beri</h2>'
                      f'<p class="sub prose">{_e(d["since"])} tarihli rapora göre '
                      f'durum değişikliği yok.</p></section>')
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
            f'Anlık durum tablosu aşağıda; burası sadece <b>hareket edenler</b>.</p>'
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
      <span class="v s-flying">{n('flying')}</span><span class="n">havada görüldü, kesinti yok</span></div>
    <div class="stat"><span class="k">Kısmen kesti</span>
      <span class="v s-partial">{n('partial')}</span><span class="n">uçuyor ama hat kesmiş</span></div>
    <div class="stat"><span class="k">Durdurdu</span>
      <span class="v s-stopped">{n('stopped')}</span><span class="n">görülmedi + basın doğruluyor</span></div>
    <div class="stat"><span class="k">Tarifede var</span>
      <span class="v s-scheduled">{n('scheduled')}</span><span class="n">kapsama dışı, tarifesi duruyor</span></div>
    <div class="stat"><span class="k">Bilinmiyor</span>
      <span class="v s-unknown">{n('unknown')}</span><span class="n">iki kaynak da sessiz</span></div>
    <div class="stat"><span class="k">Kapsama</span>
      <span class="v">{cov['score']}</span>
      <span class="n">{_e(cov['verdict'])} · {cov['control_flights']} kontrol seferi</span></div>
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
      bağlayabilmek için. Duyuru gözlem değildir.</div></div>
    <div class="card"><div class="hd"><span class="fir">Web araması</span></div>
      <div class="place"><b>Son çare.</b> Yalnızca ilk üç kaynağın da sessiz
      kaldığı havayolları için çalışır ve <b>durumu değiştirmez</b> — sadece
      kaynaklı bir not ekler. En zayıf kaynak: model enum'dan seçmek yerine
      düz metin yazar, yani yanılabilir. Yanındaki numaralar kaynak
      bağlantılarıdır, kontrol için oradalar.</div></div>
  </div>
  <p class="sub prose">Bir çelişki olduğunda <b>gözlem duyuruyu yener.</b> Uçarken
  görülen bir havayolu, basın “kesti” dese bile “Durdurdu” sayılmaz; “Kısmen
  kesti” olur. Durumların anlamı:</p>
  <div class="scroll"><table>
    <thead><tr><th>Durum</th><th>Ne demek</th><th>Nasıl karar verildi</th></tr></thead>
    <tbody>
      <tr><td>{_pill('flying')}</td>
        <td>Havayolu izlenen havalimanlarında uçarken görüldü ve kesinti
        bildirimi yok.</td><td class="why">ADS-B'de sefer var, basında kesinti haberi yok</td></tr>
      <tr class="r-partial"><td>{_pill('partial')}</td>
        <td>Hâlâ uçuyor, ama bazı hatlarını kestiği bildiriliyor. Çatışma
        döneminde en yaygın durum bu.</td>
        <td class="why">ADS-B'de sefer var + basında kesinti haberi var</td></tr>
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
    havayolu otomatik olarak “Bilinmiyor” sayılır, “Durdurdu” değil.</p></div>
</section>

<section>
  <h2>Havalimanları</h2>
  <p class="sub prose">Sorunun diğer yarısı: <i>bu havalimanına hâlâ uçuluyor
  mu?</i> Önce sorunlular. <b>Tarifeli</b> olanlar ADS-B kapsamımızın dışında —
  uçuş verisi göremiyoruz ama havayolları tarifelerinde sefer tutuyor.</p>
  <div class="scroll"><table>
    <thead><tr><th>Havalimanı</th><th>Durum</th><th class="num">Gözlenen sefer</th>
      <th class="num">Havayolu</th><th class="num">Tarifede/hafta</th>
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
  <p class="sub prose"><b>Gözlenen / tarifeli</b> sütunu asıl cevabı verir:
  havayolunun izlenen havalimanları arasında gerçekten uçtuğu haftalık sefer
  sayısı, tarifesinde planladığı sayıya bölünmüş. %100'e yakınsa tarifesini
  uyguluyor; düşükse fiilen kesmiş demektir. İki taraf da <b>aynı şehir
  çiftlerini</b> sayar, yoksa uzun menzilli havayolları haksız yere düşük
  görünür. Kapsama testini geçen gün sayısı {MIN_RATIO_DAYS}'in altındaysa
  yüzde <b>yayınlanmaz</b> — iki ham sayı gösterilir, çünkü birkaç günlük
  veriden haftalık oran çıkarmak tahminden ibarettir.</p>
  <div class="scroll"><table>
    <thead><tr><th>Kod</th><th>Havayolu ve basında çıkanlar</th><th>Durum</th>
      <th class="num">Sefer</th><th>Nerede görüldü</th>
      <th class="num">Gözlenen / tarifeli</th></tr></thead>
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
      bağlantıyı önünüze koyar.</dd>
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
