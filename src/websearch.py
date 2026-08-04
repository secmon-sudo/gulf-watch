"""Web search, for the carriers every other source is silent about.

Last resort by design. ADS-B says what flew, schedules say what is planned,
headlines say what was announced -- and for a handful of carriers all three
say nothing at all, which leaves a row reading "Bilinmiyor" and no way for the
reader to find out more.

This is weaker evidence than the other three and is treated as such: it never
changes a carrier's state, it only attaches a cited note to it. The model
writes prose rather than picking from an enum, so it can be confidently wrong
in a way the headline classifier cannot, and the citations are the point --
they are what the reader checks.

Cached per carrier per day. A search has no stable identity the way a headline
URL does, so re-running the report on the same day gives the same note.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .classify import _key

LOG = logging.getLogger("gulfwatch.websearch")

BASE = "https://api.mistral.ai/v1"
MODEL = "mistral-medium-latest"

QUESTION = (
    "As of {today}, is {name} still operating flights to the Gulf and the wider "
    "Middle East (Dubai, Doha, Bahrain, Kuwait, Riyadh, Jeddah, Amman, Beirut, "
    "Baghdad, Erbil)? Has it suspended or resumed any routes there? Answer in at "
    "most two sentences, name specific airports and dates where reported, and say "
    "plainly if it is unclear. Do not guess."
)


def _agent(key: str) -> str | None:
    try:
        r = requests.post(
            f"{BASE}/agents",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": MODEL, "name": "gulfwatch-websearch",
                  "description": "Reports whether a carrier still serves the Gulf.",
                  "tools": [{"type": "web_search"}]}, timeout=60)
        r.raise_for_status()
        return r.json()["id"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        LOG.warning("could not create the search agent: %s", exc)
        return None


def resolve(conn, carrier: str, name: str, agent_id: str | None = None) -> dict | None:
    """Cached note for one carrier. None means no answer; caller shows nothing."""
    day = datetime.now(tz=timezone.utc).date().isoformat()
    hit = conn.execute(
        "SELECT note, sources FROM carrier_note WHERE carrier=? AND day=?",
        (carrier, day)).fetchone()
    if hit:
        return {"note": hit["note"],
                "sources": [u for u in (hit["sources"] or "").split("\n") if u]}

    key = _key()
    if not key or not agent_id:
        return None

    try:
        r = requests.post(
            f"{BASE}/conversations",
            headers={"Authorization": f"Bearer {key}"},
            json={"agent_id": agent_id,
                  "inputs": QUESTION.format(today=day, name=name)}, timeout=180)
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as exc:
        LOG.warning("web search failed for %s: %s", carrier, exc)
        return None

    text, sources = [], []
    for out in payload.get("outputs") or []:
        if out.get("type") != "message.output":
            continue
        content = out.get("content")
        for chunk in (content if isinstance(content, list)
                      else [{"type": "text", "text": content}]):
            if chunk.get("type") == "text":
                text.append(chunk.get("text") or "")
            elif chunk.get("type") == "tool_reference" and chunk.get("url"):
                sources.append(chunk["url"])

    note = " ".join("".join(text).split())[:400]
    if not note:
        return None

    conn.execute(
        """INSERT OR REPLACE INTO carrier_note (carrier, day, note, sources, model)
           VALUES (?,?,?,?,?)""",
        (carrier, day, note, "\n".join(dict.fromkeys(sources)), MODEL))
    conn.commit()
    LOG.info("%s: web note (%s sources)", carrier, len(sources))
    return {"note": note, "sources": list(dict.fromkeys(sources))}


def agent(key_present: bool = True) -> str | None:
    key = _key()
    return _agent(key) if key else None
