"""Official advisory layer: EASA Conflict Zone Information Bulletins.

CZIBs are the regulatory ground truth for "should anyone be flying here", and
almost nobody wires them into a monitor. A revision bump (R12 -> R13) or a
validity extension is itself a signal and is treated as an event.

Free, no key, no registration. Be polite: one request per bulletin, run hourly
at most, honour the cache headers.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

import requests

LOG = logging.getLogger("gulfwatch.advisories")

CZIB_INDEX = "https://www.easa.europa.eu/en/domains/air-operations/czibs"

# Bulletins relevant to this monitor. Add or remove as EASA re-issues them;
# the index page above lists the current set.
WATCHED = [
    "czib-2026-04",   # Iran
    "czib-2026-05",   # Iraq
    "czib-2026-06",   # Lebanon
]

_session = requests.Session()
_session.headers["User-Agent"] = "gulfwatch/1.0 (personal research)"

VALID_RE = re.compile(r"valid\s*(?:un)?til[:\s]*([0-9]{1,2}\s+\w+\s+[0-9]{4})", re.I)
REV_RE = re.compile(r"\bR(\d{1,2})\b")


def _strip_html(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch(conn) -> list[dict]:
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    changed = []
    for slug in WATCHED:
        url = f"{CZIB_INDEX}/{slug}"
        try:
            resp = _session.get(url, timeout=30)
        except requests.RequestException as exc:
            LOG.warning("czib fetch failed for %s: %s", slug, exc)
            continue
        if resp.status_code != 200:
            LOG.warning("czib %s -> HTTP %s", slug, resp.status_code)
            continue

        text = _strip_html(resp.text)
        body_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        ref = slug.upper()
        rev_match = REV_RE.search(text[:4000])
        revision = rev_match.group(0) if rev_match else None
        valid_match = VALID_RE.search(text)
        valid_to = valid_match.group(1) if valid_match else None
        title = text[:120]

        prev = conn.execute(
            "SELECT body_hash, revision FROM advisory WHERE source='easa_czib' AND ref=?",
            (ref,),
        ).fetchone()

        conn.execute(
            """INSERT INTO advisory (source, ref, revision, title, valid_to,
                                     body_hash, url, first_seen, last_seen)
               VALUES ('easa_czib',?,?,?,?,?,?,?,?)
               ON CONFLICT(source, ref) DO UPDATE SET
                 revision=excluded.revision, title=excluded.title,
                 valid_to=excluded.valid_to, body_hash=excluded.body_hash,
                 last_seen=excluded.last_seen""",
            (ref, revision, title, valid_to, body_hash, url, now, now),
        )

        if prev and prev["body_hash"] != body_hash:
            changed.append({"ref": ref, "url": url, "revision": revision,
                            "valid_to": valid_to, "previous_revision": prev["revision"]})
            LOG.info("CZIB %s changed", ref)

    conn.commit()
    return changed


def current(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT ref, revision, valid_to, url, last_seen FROM advisory "
        "WHERE source='easa_czib' ORDER BY ref"
    ).fetchall()
    return [dict(r) for r in rows]
