"""The shared OpenSky allowance record.

Every job that spends the OpenSky credential passes through this table before
it fetches anything. `concurrency: group: ingest` does not do that job: it
queues runs so two of them never write the database at once, which says
nothing about the allowance. The harvest that queues first still leaves the
ingest behind it with an empty quota, and that is exactly what happened
between 2026-08-25 and 09-02 -- three of five ingests logged `legs=0`, each
one starting with `OpenSky daily allowance exhausted`, and `observed_days`
fell to 2 of the required 5.

The record holds the two facts CLAUDE.md asks for and nothing else: when the
allowance was last refused, and how long OpenSky itself said to wait. Nothing
here guesses at a reset hour, because there is not one -- the window rolls
from whenever the credits were spent, measured 2026-09-02 (13:21 UTC, nothing
having run that day, `retry in 4.5h`).

It lives in the database, so it is shared exactly as far as the database is:
both workflows download `db-latest` before they run and upload it after, so a
denial one of them records is read by the next. A job killed before its upload
step loses the record, and a local run against a local copy is not in the
conversation at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("gulfwatch.quota")

PROVIDER = "opensky"


def wait_seconds(conn, provider: str = PROVIDER, now: datetime | None = None) -> int:
    """Seconds the allowance is known to be spent for. 0 means go ahead."""
    row = conn.execute(
        "SELECT spent_at, resets_at, consumer FROM quota_state WHERE provider = ?",
        (provider,),
    ).fetchone()
    if row is None:
        return 0
    now = now or datetime.now(tz=timezone.utc)
    left = (datetime.fromisoformat(row["resets_at"]) - now).total_seconds()
    if left <= 0:
        return 0
    LOG.warning("%s allowance was spent at %s by %s; %.1fh left on the window",
                provider, row["spent_at"], row["consumer"] or "?", left / 3600)
    return int(left)


def record_denial(conn, retry_after: int, consumer: str,
                  provider: str = PROVIDER, now: datetime | None = None) -> str:
    """File a refusal so the next job does not walk into the same wall.

    The newest observation wins outright rather than being maxed with the one
    it replaces: OpenSky's retry-after is the only measurement of a window
    nobody else can see, and a fresher one describes it better.
    """
    now = now or datetime.now(tz=timezone.utc)
    resets_at = (now + timedelta(seconds=int(retry_after))).isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR REPLACE INTO quota_state
           (provider, spent_at, resets_at, retry_after, consumer)
           VALUES (?,?,?,?,?)""",
        (provider, now.isoformat(timespec="seconds"), resets_at,
         int(retry_after), consumer),
    )
    conn.commit()
    LOG.warning("%s allowance spent by %s; nothing may fetch until %s",
                provider, consumer, resets_at)
    return resets_at
