"""Official advisory layer: EASA Conflict Zone Information Bulletins.

CZIBs are the regulatory ground truth for "should anyone be flying here", and
almost nobody wires them into a monitor. A re-issue or a validity extension is
itself a signal and is treated as an event. EASA keeps the CZIB number stable
across re-issues and moves the issue date instead, so `revision` here holds the
issue date rather than an R-number.

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

# The bulletin's own fields, as EASA renders them:
#   Status Active CZIB number CZIB-2026-04 Issue date 08/07/2026
#   Valid until 31/08/2026, unless reviewed earlier.
# Dates are DD/MM/YYYY. Note there is no "R12"-style revision anywhere on these
# pages -- EASA re-issues a CZIB under the same number with a new issue date,
# so the issue date IS the revision identifier.
STATUS_RE = re.compile(r"Status\s+(\w+)\s+CZIB number", re.I)
ISSUED_RE = re.compile(r"Issue date\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
VALID_RE = re.compile(r"Valid until\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.I | re.S)

# Everything before this is the site's navigation menu, which changes when EASA
# redesigns the site and would otherwise register as a bulletin revision.
CONTENT_START_RE = re.compile(r"Status\s+\w+\s+CZIB number", re.I)


def _iso(dmy: str | None) -> str | None:
    """31/08/2026 -> 2026-08-31. Left as-is if EASA ever changes the format."""
    if not dmy:
        return None
    try:
        return datetime.strptime(dmy, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return dmy


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
        ref = slug.upper()

        # Hash the bulletin, not the navigation chrome wrapped around it.
        start = CONTENT_START_RE.search(text)
        body = text[start.start():] if start else text
        body_hash = hashlib.sha256(body.encode()).hexdigest()[:16]

        issued = ISSUED_RE.search(text)
        revision = _iso(issued.group(1)) if issued else None
        valid_match = VALID_RE.search(text)
        valid_to = _iso(valid_match.group(1)) if valid_match else None
        status = STATUS_RE.search(text)
        head = TITLE_RE.search(resp.text)
        title = head.group(1).split("|")[0].strip() if head else text[:120]
        if status:
            title = f"{title} ({status.group(1).lower()})"

        if not (issued and valid_match):
            LOG.warning("czib %s: could not read issue date / valid until -- "
                        "EASA may have changed the page layout", ref)

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
