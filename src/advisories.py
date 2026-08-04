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


def _summarise(conn, ref: str, title: str, body: str, body_hash: str) -> str | None:
    """One plain sentence saying what the bulletin actually warns about.

    The bulletin text was already being downloaded to hash it for change
    detection and then thrown away, so the report could name a CZIB but not
    say what it was about. Only re-read when the bulletin itself changed --
    the hash it was written from is stored alongside it.
    """
    row = conn.execute(
        "SELECT summary, summary_hash FROM advisory WHERE source='easa_czib' AND ref=?",
        (ref,)).fetchone()
    if row and row["summary"] and row["summary_hash"] == body_hash:
        return row["summary"]

    from .classify import _key
    key = _key()
    if not key:
        return row["summary"] if row else None

    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "ministral-8b-latest", "temperature": 0,
                  "messages": [
                      {"role": "system", "content":
                       "Summarise an EASA Conflict Zone Information Bulletin in ONE "
                       "plain Turkish sentence, max 30 words. Say which airspace, "
                       "what the risk is, and any altitude limit given. Use only what "
                       "the text states. Output the sentence and nothing else: no "
                       "markdown, no asterisks, no bold, no prefix, no preamble."},
                      {"role": "user", "content": f"{title}\n\n{body}"}]},
            timeout=60)
        resp.raise_for_status()
        out = resp.json()["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
        LOG.warning("czib summary failed for %s: %s", ref, exc)
        return row["summary"] if row else None
    # The model reaches for markdown bold and the odd stray prefix however the
    # prompt is worded, and this lands in an HTML table cell.
    out = re.sub(r"[*_`#]+", "", out)
    out = re.sub(r"^\s*\w+['\u2019]?\w*\s*:\s*(?=[A-ZÇĞİÖŞÜ])", "", out)
    return " ".join(out.split())[:300]


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
        body_text = body[:4000]
        if status:
            title = f"{title} ({status.group(1).lower()})"

        if not (issued and valid_match):
            LOG.warning("czib %s: could not read issue date / valid until -- "
                        "EASA may have changed the page layout", ref)

        prev = conn.execute(
            "SELECT body_hash, revision FROM advisory WHERE source='easa_czib' AND ref=?",
            (ref,),
        ).fetchone()

        summary = _summarise(conn, ref, title, body_text, body_hash)
        conn.execute(
            """INSERT INTO advisory (source, ref, revision, title, valid_to,
                                     body_hash, url, first_seen, last_seen,
                                     summary, summary_hash)
               VALUES ('easa_czib',?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source, ref) DO UPDATE SET
                 revision=excluded.revision, title=excluded.title,
                 valid_to=excluded.valid_to, body_hash=excluded.body_hash,
                 last_seen=excluded.last_seen, summary=excluded.summary,
                 summary_hash=excluded.summary_hash""",
            (ref, revision, title, valid_to, body_hash, url, now, now,
             summary, body_hash),
        )

        if prev and prev["body_hash"] != body_hash:
            changed.append({"ref": ref, "url": url, "revision": revision,
                            "valid_to": valid_to, "previous_revision": prev["revision"]})
            LOG.info("CZIB %s changed", ref)

    conn.commit()
    return changed


def current(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT ref, title, revision, valid_to, url, last_seen, summary FROM advisory "
        "WHERE source='easa_czib' ORDER BY ref"
    ).fetchall()
    return [dict(r) for r in rows]
