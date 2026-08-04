"""Operator report. Run it by hand when you want to know where things stand.

    python -m src.report                 # writes public/report.html
    python -m src.report --no-news       # skip the news sweep (faster, offline)
    python -m src.report --days 14

Two sources answer the same question from different sides, because neither is
enough on its own:

  ADS-B  tells you what actually flew, but only where volunteers run receivers.
         Measured live, that is good over the UAE, Qatar, Bahrain and Jordan
         and effectively blind over Saudi Arabia, Kuwait, Iraq and Iran.
  News   tells you what was announced, everywhere, but only for carriers big
         enough to be written about, and an announcement is not an observation.

Every cell in the report says which of the two it came from. A carrier we
cannot see and nobody wrote about is reported as unknown, not as stopped.
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
from pathlib import Path

from . import advisories, config, db, firwatch, metrics
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


def carrier_news(name: str, limit: int = 4) -> list[dict]:
    """Headlines that name this carrier, newest first.

    Google News answers a carrier query with regional round-ups far more often
    than with anything about the carrier, so anything that does not name it in
    the title is dropped rather than attributed to it.
    """
    items = _news(f'"{name}" flights suspended OR cancelled OR resumed '
                  f'Middle East OR Gulf OR Dubai OR Doha OR Qatar', limit=10)
    keys = _aliases(name)
    out = []
    for it in items:
        title = it["title"]
        low = title.lower()
        if not any(k in low for k in keys) or GENERIC.search(title):
            continue
        it["signal"] = ("resumed" if RESUMED.search(title)
                        else "stopped" if STOPPED.search(title) else "mentioned")
        out.append(it)
        if len(out) >= limit:
            break
    return out


def verdict(seen_at: dict, news: list[dict]) -> tuple[str, str]:
    """(state, why). ADS-B outranks news: observation beats announcement."""
    if seen_at:
        n = sum(seen_at.values())
        where = ", ".join(sorted(seen_at))
        if any(x["signal"] == "stopped" for x in news):
            return "partial", f"seen on {n} legs at {where}, but reporting says some routes are cut"
        return "flying", f"seen on {n} legs at {where}"
    if any(x["signal"] == "stopped" for x in news):
        return "stopped", "not seen, and reporting says flights are suspended"
    if any(x["signal"] == "resumed" for x in news):
        return "unknown", "not seen, though reporting says flights resumed"
    return "unknown", "not seen in our coverage, and nothing reported"


def collect(days: int, with_news: bool) -> dict:
    conn = db.connect()
    ref = metrics.reference_day()
    act = activity(conn, days)
    base = baseline_weekly(conn)
    carriers = config.tracked_carriers()

    rows = []
    for code, cfg in sorted(carriers.items(), key=lambda kv: kv[1]["name"]):
        news = []
        if with_news:
            news = carrier_news(cfg["name"])
            time.sleep(1.2)          # be polite to Google News
            LOG.info("%s: %s headlines", code, len(news))
        seen_at = dict(act["seen"].get(code, {}))
        state, why = verdict(seen_at, news)
        rows.append({"code": code, "name": cfg["name"],
                     "iata": cfg.get("iata"), "country": cfg.get("country"),
                     "seen_at": seen_at, "legs": sum(seen_at.values()),
                     "baseline": base.get(code), "news": news,
                     "state": state, "why": why})

    return {
        "generated_at": datetime.now(tz=timezone.utc),
        "as_of_day": ref.isoformat(),
        "window": (act["since"], act["until"]),
        "days": days,
        "coverage": metrics.score_coverage(conn, ref),
        "baseline_ready": bool(base),
        "carriers": rows,
        "airports": config.airports(),
        "firs": firwatch.summary(conn, days=7),
        "advisories": advisories.current(conn),
        "with_news": with_news,
    }


# --- Render ----------------------------------------------------------------

STATE_LABEL = {"flying": "Flying", "partial": "Partly cut",
               "stopped": "Stopped", "unknown": "Unknown"}

CSS = """
:root{
  --ink:#10161D; --ink-2:#3C4854; --ink-3:#6B7887;
  --bg:#F4F6F8; --panel:#FFFFFF; --rule:#DDE3E9;
  --accent:#2D6A9F; --accent-soft:#E7EFF6;
  --flying:#2F7D6B; --partial:#B0761C; --stopped:#B0433A; --unknown:#7A8794;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{ --ink:#E4EAF0; --ink-2:#A9B5C2; --ink-3:#76838F;
    --bg:#0F141A; --panel:#161D25; --rule:#25303B;
    --accent:#6FA8D6; --accent-soft:#1B2733;
    --flying:#5FB89F; --partial:#D9A445; --stopped:#DE7268; --unknown:#76838F; }
}
:root[data-theme="dark"]{ --ink:#E4EAF0; --ink-2:#A9B5C2; --ink-3:#76838F;
  --bg:#0F141A; --panel:#161D25; --rule:#25303B;
  --accent:#6FA8D6; --accent-soft:#1B2733;
  --flying:#5FB89F; --partial:#D9A445; --stopped:#DE7268; --unknown:#76838F; }
:root[data-theme="light"]{ --ink:#10161D; --ink-2:#3C4854; --ink-3:#6B7887;
  --bg:#F4F6F8; --panel:#FFFFFF; --rule:#DDE3E9;
  --accent:#2D6A9F; --accent-soft:#E7EFF6;
  --flying:#2F7D6B; --partial:#B0761C; --stopped:#B0433A; --unknown:#7A8794; }

*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:40px 24px 72px;
  display:flex;flex-direction:column;gap:40px}
.prose{max-width:64ch}
h1,h2,h3{text-wrap:balance;margin:0}
h1{font-family:var(--mono);font-size:clamp(21px,3.4vw,29px);font-weight:600;
  letter-spacing:.10em;text-transform:uppercase}
h2{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.16em;
  text-transform:uppercase;color:var(--ink-3);
  padding-bottom:8px;border-bottom:1px solid var(--rule)}
h3{font-size:16px;font-weight:600}
p{margin:0}
a{color:var(--accent)}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--ink-3)}
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


def _pill(state: str) -> str:
    return (f'<span class="pill s-{state}"><span class="dot"></span>'
            f'{STATE_LABEL[state]}</span>')


def _rows(data: dict) -> str:
    ap = data["airports"]
    out = []
    order = {"stopped": 0, "partial": 1, "unknown": 2, "flying": 3}
    for c in sorted(data["carriers"], key=lambda r: (order[r["state"]], -r["legs"])):
        at = " ".join(f'{ap[i]["iata"]}·{n}' for i, n in
                      sorted(c["seen_at"].items(), key=lambda kv: -kv[1])) or "—"
        heads = "".join(
            f'<div class="head"><span class="tag s-{h["signal"]}">{_e(h["signal"])}</span>'
            f'<a href="{_e(h["url"])}" target="_blank" rel="noopener">{_e(h["title"])}</a></div>'
            for h in c["news"])
        heads = f'<div class="heads">{heads}</div>' if heads else ""
        cls = f' class="r-{c["state"]}"' if c["state"] in ("stopped", "partial") else ""
        out.append(
            f'<tr{cls}>'
            f'<td><div class="code">{_e(c["code"])}</div>'
            f'<div class="meta">{_e(c["iata"] or "")} {_e(c["country"] or "")}</div></td>'
            f'<td><div class="name">{_e(c["name"])}</div>{heads}</td>'
            f'<td>{_pill(c["state"])}<div class="why">{_e(c["why"])}</div></td>'
            f'<td class="num">{c["legs"] or "—"}</td>'
            f'<td class="at">{_e(at)}</td>'
            f'</tr>')
    return "".join(out)


def render(data: dict) -> str:
    cov = data["coverage"]
    cs = data["carriers"]
    n = lambda s: sum(1 for c in cs if c["state"] == s)  # noqa: E731
    gen = data["generated_at"].strftime("%Y-%m-%d %H:%M UTC")

    warn = []
    if not data["baseline_ready"]:
        warn.append(
            "<b>No baseline yet.</b> Until <code>backfill-baseline</code> "
            "completes, this report can say whether a carrier was seen flying, "
            "but not whether it is flying <i>less than it used to</i>. "
            "Frequency comparisons appear here once the reference period lands.")
    if not data["with_news"]:
        warn.append("<b>News sweep skipped.</b> Carriers outside ADS-B coverage "
                    "have no second source in this run.")
    warn.append(
        "<b>Absence is not proof.</b> ADS-B coverage is thin to nonexistent over "
        "Saudi Arabia, Kuwait, Iraq and Iran, so a carrier can be flying there and "
        "still show no legs here. Rows read <i>Stopped</i> only when reporting "
        "agrees; otherwise they read <i>Unknown</i>.")

    fircards = "".join(
        f'<div class="card"><div class="hd"><span class="fir">{_e(f["fir"])}</span>'
        f'<span class="cnt">{f["total_transits"]}</span></div>'
        f'<div class="place">{_e(f["name"])}'
        + (' <span class="watch">CZIB</span>' if f.get("czib_watch") else "")
        + '</div><div class="who">'
        + (" ".join(_e(c["carrier"]) for c in f["carriers"]) or "—")
        + "</div></div>"
        for f in sorted(data["firs"], key=lambda x: -x["total_transits"]))

    adv = "".join(
        f'<div class="head"><span class="tag s-partial">{_e(a["ref"])}</span>'
        f'<a href="{_e(a["url"])}" target="_blank" rel="noopener">{_e(a["title"] or a["ref"])}</a>'
        f' <span class="meta">issued {_e(a["revision"])} · valid to {_e(a["valid_to"])}</span>'
        f"</div>" for a in data["advisories"]) or '<p class="sub">None recorded.</p>'

    return f"""<title>GulfWatch — operator report {_e(data['as_of_day'])}</title>
<style>{CSS}</style>
<div class="wrap">
<header>
  <div class="eyebrow">GulfWatch · manual run</div>
  <h1>Middle East carrier operations</h1>
  <p class="sub prose">Whether each tracked carrier is still operating at the
  monitored Gulf and Levant airports, from ADS-B observation and from reporting.
  Data as of <b>{_e(data['as_of_day'])}</b>; observation window
  {_e(data['window'][0])} → {_e(data['window'][1])} ({data['days']} days).
  Generated {_e(gen)}.</p>
</header>

<section>
  <div class="band">
    <div class="stat"><span class="k">Carriers</span><span class="v">{len(cs)}</span>
      <span class="n">tracked</span></div>
    <div class="stat"><span class="k">Flying</span>
      <span class="v s-flying">{n('flying')}</span><span class="n">seen on ADS-B</span></div>
    <div class="stat"><span class="k">Partly cut</span>
      <span class="v s-partial">{n('partial')}</span><span class="n">seen, routes reported cut</span></div>
    <div class="stat"><span class="k">Stopped</span>
      <span class="v s-stopped">{n('stopped')}</span><span class="n">unseen + reported</span></div>
    <div class="stat"><span class="k">Unknown</span>
      <span class="v s-unknown">{n('unknown')}</span><span class="n">no signal either way</span></div>
    <div class="stat"><span class="k">Coverage</span>
      <span class="v">{cov['score']}</span><span class="n">{_e(cov['verdict'])} · {cov['control_flights']} control legs</span></div>
  </div>
  {"".join(f'<div class="notice"><span class="t">Read this first</span><p>{w}</p></div>' for w in warn)}
</section>

<section>
  <h2>Carriers</h2>
  <div class="scroll"><table>
    <thead><tr><th>Code</th><th>Carrier &amp; reporting</th><th>Status</th>
      <th class="num">Legs</th><th>Seen at</th></tr></thead>
    <tbody>{_rows(data)}</tbody>
  </table></div>
</section>

<section>
  <h2>Overflights, last 7 days</h2>
  <p class="sub prose">Distinct aircraft sampled inside each FIR from live ADS-B,
  after discarding positions whose navigation integrity was too low to trust —
  the Gulf sees sustained GNSS interference. <b>CZIB</b> marks airspace EASA
  tells operators to avoid.</p>
  <div class="cards">{fircards}</div>
</section>

<section>
  <h2>EASA conflict-zone bulletins</h2>
  <div class="heads">{adv}</div>
</section>

<section>
  <h2>Sources</h2>
  <dl class="src">
    <dt>Flights</dt><dd>OpenSky Network — non-commercial use. Volunteer receiver
      coverage, strong over the UAE, Qatar, Bahrain and Jordan; thin to absent
      over Saudi Arabia, Kuwait, Iraq and Iran.</dd>
    <dt>Live ADS-B</dt><dd>adsb.lol and airplanes.live, ODbL 1.0.</dd>
    <dt>Reporting</dt><dd>Google News RSS. Headline keyword matching, filtered to
      items naming the carrier. It hands you the link; it does not decide.</dd>
    <dt>Advisories</dt><dd>EASA Conflict Zone Information Bulletins.</dd>
  </dl>
</section>

<footer>
  <p>Absence of data is never evidence of absence of flights. Codeshares and wet
  leases are invisible to ADS-B: a flight marketed by one carrier and operated by
  another counts as the operator.</p>
  <p>This is a personal research tool, not an operational decision-making system.
  For flight planning the sources are the AIP, the NOTAM system, and your
  operator's own risk assessment.</p>
</footer>
</div>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    data = collect(args.days, with_news=not args.no_news)
    path = Path(args.out or (config.PUBLIC_DIR / "report.html"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    LOG.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
