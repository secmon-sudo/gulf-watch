"""Optional Telegram push for stop and resume events.

Does nothing unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both set, so
the module is safe to call unconditionally from the ETL.

No coverage gating here on purpose: suspensions.detect() already refuses to
open or close an event on a day when coverage is not `ok`, so anything that
reaches this function was found on a day the data could be trusted.
"""

from __future__ import annotations

import logging
import os

import requests

LOG = logging.getLogger("gulfwatch.notify")

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram rejects messages over 4096 characters. A run that opens dozens of
# events is a story about coverage, not about airlines, so list a few and count
# the rest.
MAX_LINES = 12


def _line(ev: dict, resumed: bool) -> str:
    where = f"{ev['scope']} {ev['detail']}"
    if resumed:
        return f"- {ev['carrier']} — {where}, back {ev['resumed_on']} after {ev['days_stopped']}d"
    return (f"- {ev['carrier']} — {where}, silent since {ev['started_on']} "
            f"({ev['days_stopped']}d, baseline {ev['baseline_weekly']}/wk)")


def format_message(events: dict, day: str) -> str:
    parts = [f"GulfWatch {day}"]
    for title, key, resumed in (("STOPPED", "opened_events", False),
                                ("RESUMED", "resumed_events", True)):
        evs = events.get(key) or []
        if not evs:
            continue
        parts.append("")
        parts.append(title)
        parts.extend(_line(e, resumed) for e in evs[:MAX_LINES])
        if len(evs) > MAX_LINES:
            parts.append(f"...and {len(evs) - MAX_LINES} more")
    return "\n".join(parts)


def send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        LOG.debug("telegram not configured; skipping push")
        return False
    try:
        r = requests.post(API.format(token=token),
                          json={"chat_id": chat, "text": text,
                                "disable_web_page_preview": True},
                          timeout=15)
        r.raise_for_status()
    except requests.RequestException as exc:
        # A failed notification must never fail the run that produced the data.
        LOG.warning("telegram push failed: %s", exc)
        return False
    return True


def announce(events: dict, day: str) -> bool:
    if not (events.get("opened_events") or events.get("resumed_events")):
        return False
    return send(format_message(events, day))
