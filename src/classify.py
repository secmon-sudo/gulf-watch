"""Headline classification with a small LLM, where keywords cannot reach.

The regex layer asks "does this headline contain a suspension word". That
cannot bind a verb to a subject, so a headline reading

    "Emirates, flydubai maintain schedule, Etihad, Qatar Airways face
     cancellations"

marks flydubai as cut when it says the exact opposite. Nor can it tell you
*which* routes went: "Air Arabia cancel routes to Kuwait" yields "stopped" and
loses the word Kuwait.

Scope is deliberately narrow. This reads headline text and nothing else. Flight
counts, coverage scoring, suspension detection and frequency are arithmetic and
stay arithmetic -- a report whose numbers move between runs on the same data is
worth less than no report.

Optional: with no key, callers fall back to the regex signal.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import requests

from . import config

LOG = logging.getLogger("gulfwatch.classify")

API = "https://api.mistral.ai/v1/chat/completions"
MODEL = os.environ.get("MISTRAL_MODEL", "ministral-3b-latest")

ACTIONS = {"stopped", "resumed", "unaffected", "unclear"}


def _key() -> str | None:
    key = os.environ.get("MISTRAL_API_KEY")
    if key:
        return key
    env = config.ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("MISTRAL_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def available() -> bool:
    return bool(_key())


def _prompt() -> str:
    ap = {c["iata"]: c["city"] for c in config.airports().values()}
    vocab = ", ".join(f"{k}={v}" for k, v in ap.items())
    # The airport vocabulary is not decoration. Asked for free-form IATA codes
    # the model answered "KUW" for Kuwait, which is not a real code; given the
    # list it answers KWI.
    return f"""Classify what an airline news headline says about ONE named carrier.
The carrier is already known to appear in the headline.

Return ONLY JSON:
{{"action":"stopped"|"resumed"|"unaffected"|"unclear","airports":[...],"why":"<8 words"}}

action = what the headline says about THIS carrier, not about others named in
  it. If it says this carrier maintains or keeps its schedule while OTHERS
  cancel -> "unaffected". If the headline is about the region generally and
  says nothing specific about this carrier -> "unclear".

airports = ONLY codes from this list, for places the headline explicitly ties
to this carrier. Use the exact code. Empty list if none apply.
{vocab}"""


def classify(conn, carrier: str, name: str, headline: dict) -> dict | None:
    """Cached classification. None means "no answer, use the regex signal"."""
    url = headline.get("url") or ""
    cached = conn.execute(
        "SELECT action, airports, why FROM headline_class WHERE url=? AND carrier=?",
        (url, carrier)).fetchone()
    if cached:
        return {"action": cached["action"],
                "airports": [a for a in (cached["airports"] or "").split(",") if a],
                "why": cached["why"], "cached": True}

    key = _key()
    if not key:
        return None

    try:
        resp = requests.post(
            API, headers={"Authorization": f"Bearer {key}"},
            json={"model": MODEL, "temperature": 0,
                  "response_format": {"type": "json_object"},
                  "messages": [
                      {"role": "system", "content": _prompt()},
                      {"role": "user",
                       "content": f"Carrier: {name}\nHeadline: {headline['title']}"}]},
            timeout=45)
        resp.raise_for_status()
        out = json.loads(resp.json()["choices"][0]["message"]["content"])
    except (requests.RequestException, ValueError, KeyError) as exc:
        LOG.warning("classify failed for %s: %s", carrier, exc)
        return None

    action = out.get("action")
    if action not in ACTIONS:
        LOG.warning("classify returned action=%r for %s; ignoring", action, carrier)
        return None

    valid = {c["iata"] for c in config.airports().values()}
    airports = [a for a in (out.get("airports") or []) if a in valid]
    why = str(out.get("why") or "")[:80]

    conn.execute(
        """INSERT OR REPLACE INTO headline_class
           (url, carrier, action, airports, why, model, classified_at)
           VALUES (?,?,?,?,?,?,?)""",
        (url, carrier, action, ",".join(airports), why, MODEL,
         datetime.now(tz=timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    return {"action": action, "airports": airports, "why": why, "cached": False}
