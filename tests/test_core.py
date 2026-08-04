"""Tests for the logic that fails silently if it is wrong.

Run: python -m pytest tests -q   (or: python tests/test_core.py)
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, db, metrics, notify  # noqa: E402
from src.parse import is_freight, parse_callsign  # noqa: E402


class TestCallsign(unittest.TestCase):
    def test_prefix_not_iata(self):
        # The classic mistake: using IATA codes to match ADS-B callsigns.
        self.assertEqual(parse_callsign("PGT751 "), ("PGT", 751))
        self.assertEqual(parse_callsign("KNE1234"), ("KNE", 1234))
        self.assertEqual(parse_callsign("SVA23"), ("SVA", 23))
        self.assertEqual(parse_callsign("QTR8"), ("QTR", 8))

    def test_registration_is_not_a_carrier(self):
        for cs in ("A6-EDC", "N512UP", "", None, "TC AJP"):
            self.assertEqual(parse_callsign(cs), (None, None))

    def test_alpha_suffix(self):
        self.assertEqual(parse_callsign("MEA212A"), ("MEA", 212))

    def test_every_configured_carrier_is_a_valid_prefix(self):
        for code in config.carriers():
            self.assertRegex(code, r"^[A-Z]{3}$", f"{code} is not an ICAO prefix")


class TestFreight(unittest.TestCase):
    def test_qatar_cargo_split(self):
        cfg = config.carriers()["QTR"]
        self.assertTrue(is_freight(cfg, 8412))
        self.assertFalse(is_freight(cfg, 8))

    def test_freight_only_carrier(self):
        self.assertTrue(is_freight({"freight_only": True}, 12))


class TestClassify(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(metrics.classify(14, 14)[0], "NORMAL")
        self.assertEqual(metrics.classify(7, 14)[0], "REDUCED")
        self.assertEqual(metrics.classify(2, 14)[0], "MINIMAL")
        self.assertEqual(metrics.classify(0, 14)[0], "SUSPENDED")

    def test_no_baseline_is_not_a_suspension(self):
        # Without a baseline we must say UNKNOWN, never SUSPENDED.
        self.assertEqual(metrics.classify(0, 0)[0], "UNKNOWN")
        self.assertEqual(metrics.classify(5, 0)[0], "NEW")


class TestCoverageGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _leg(self, carrier, day, i):
        return {
            "icao24": f"aa{i:04x}", "first_seen": 1700000000 + i * 3600,
            "last_seen": None, "callsign": f"{carrier}{i}", "carrier": carrier,
            "flight_number": i, "dep_icao": "OMDB", "arr_icao": "OTHH",
            "is_freight": 0, "dep_date": day.isoformat(), "source": "test",
            "ingested_at": 0,
        }

    def test_seeing_nothing_at_all_is_never_ok(self):
        """An empty database is blindness, not health.

        Reachable in production: the OpenSky credential breaks, ingest keeps
        running because a 403 no longer aborts it, and after 28 days the
        trailing window is empty. If that scored `ok`, detect() would run
        against total silence and open a suspension for every carrier.
        """
        cov = metrics.score_coverage(self.conn, date.today())
        self.assertEqual(cov["verdict"], "outage")
        self.assertNotEqual(cov["verdict"], "ok")

    def test_cold_start_with_traffic_is_ok(self):
        # No history yet, but flights are arriving: that is a first run, not an
        # outage, and it must not suppress everything.
        today = date.today()
        db.upsert_flights(self.conn, [self._leg("UAE", today, i) for i in range(10)])
        cov = metrics.score_coverage(self.conn, today)
        self.assertEqual(cov["verdict"], "ok")

    def test_outage_is_not_reported_as_suspension(self):
        """The core safety property: sensors going dark must not look like war."""
        today = date.today()
        rows, i = [], 0
        # 28 days of healthy control traffic plus a tracked carrier
        for off in range(28, 0, -1):
            d = today - timedelta(days=off)
            for _ in range(10):
                i += 1
                rows.append(self._leg("UAE", d, i))
            for _ in range(3):
                i += 1
                rows.append(self._leg("GFA", d, i))
        # today: everything vanishes -- an outage, not a suspension
        db.upsert_flights(self.conn, rows)
        metrics.rebuild_daily(self.conn)

        cov = metrics.score_coverage(self.conn, today)
        self.assertEqual(cov["verdict"], "outage")

        report = metrics.route_report(self.conn, today)
        self.assertTrue(all(r["status"] == "UNKNOWN" for r in report["routes"]))
        self.assertEqual(metrics.alerts_from(report), [])

    def test_healthy_coverage_allows_alerts(self):
        today = date.today()
        rows, i = [], 0
        for off in range(28, -1, -1):
            d = today - timedelta(days=off)
            for _ in range(10):
                i += 1
                rows.append(self._leg("UAE", d, i))
            # GFA stops 9 days ago while controls keep flying. Note the gap
            # must exceed the 7-day rolling window, not just
            # SUSPENSION_CONFIRM_DAYS -- a route with any departure inside the
            # window reads MINIMAL, which is the honest answer.
            if off > 8:
                for _ in range(3):
                    i += 1
                    rows.append(self._leg("GFA", d, i))
        db.upsert_flights(self.conn, rows)
        metrics.rebuild_daily(self.conn)
        self.conn.execute(
            "INSERT INTO baseline VALUES ('GFA','OMDB','OTHH',21,28,'x','y')")
        self.conn.commit()

        cov = metrics.score_coverage(self.conn, today)
        self.assertEqual(cov["verdict"], "ok")
        report = metrics.route_report(self.conn, today)
        gfa = [r for r in report["routes"] if r["carrier"] == "GFA"][0]
        self.assertEqual(gfa["status"], "SUSPENDED")
        self.assertGreaterEqual(gfa["silent_days"], config.SUSPENSION_CONFIRM_DAYS)


class TestIdempotency(unittest.TestCase):
    def test_reingest_does_not_duplicate(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        conn = db.connect(tmp.name)
        row = {
            "icao24": "abc123", "first_seen": 1700000000, "last_seen": 1700007200,
            "callsign": "QTR8", "carrier": "QTR", "flight_number": 8,
            "dep_icao": "OTHH", "arr_icao": "OMDB", "is_freight": 0,
            "dep_date": "2025-11-14", "source": "opensky", "ingested_at": 0,
        }
        db.upsert_flights(conn, [row])
        db.upsert_flights(conn, [row, dict(row, last_seen=1700009000)])
        n = conn.execute("SELECT COUNT(*) c FROM flight").fetchone()["c"]
        self.assertEqual(n, 1)
        conn.close()
        os.unlink(tmp.name)


class TestGnssIntegrity(unittest.TestCase):
    def test_spoofed_positions_rejected(self):
        from src.adsb_live import position_is_trustworthy
        good = {"lat": 25.2, "lon": 51.6, "nic": 8, "sil": 3, "alt_baro": 35000}
        self.assertTrue(position_is_trustworthy(good))
        self.assertFalse(position_is_trustworthy({**good, "nic": 2}))
        self.assertFalse(position_is_trustworthy({**good, "sil": 0}))
        self.assertFalse(position_is_trustworthy({**good, "lat": None}))




class TestSuspensionEvents(unittest.TestCase):
    """The stop/resume state machine, including the ways it must NOT fire."""

    def setUp(self):
        from src import suspensions
        self.susp = suspensions
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = db.connect(self.tmp.name)
        self.today = date.today()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _fill(self, silent_for=None, coverage_bad_days=()):
        """28 days of control traffic.

        `silent_for=N` means GFA has flown nothing on the last N days, so its
        last operating day is today-N and the stop begins on today-N+1.
        """
        rows, i = [], 0
        for off in range(28, -1, -1):
            d = self.today - timedelta(days=off)
            for _ in range(10):
                i += 1
                rows.append({
                    "icao24": f"c0{i:04x}", "first_seen": 1700000000 + i * 60,
                    "last_seen": None, "callsign": f"UAE{i}", "carrier": "UAE",
                    "flight_number": i, "dep_icao": "OMDB", "arr_icao": "OTHH",
                    "is_freight": 0, "dep_date": d.isoformat(),
                    "source": "t", "ingested_at": 0})
            flying = silent_for is None or off >= silent_for
            if flying:
                for _ in range(3):
                    i += 1
                    rows.append({
                        "icao24": f"d0{i:04x}", "first_seen": 1700000000 + i * 60,
                        "last_seen": None, "callsign": f"GFA{i}", "carrier": "GFA",
                        "flight_number": i, "dep_icao": "OBBI", "arr_icao": "OMDB",
                        "is_freight": 0, "dep_date": d.isoformat(),
                        "source": "t", "ingested_at": 0})
        db.upsert_flights(self.conn, rows)
        metrics.rebuild_daily(self.conn)
        self.conn.execute(
            "INSERT INTO baseline VALUES ('GFA','OBBI','OMDB',21,28,'a','b')")
        for off in range(29):
            d = self.today - timedelta(days=off)
            metrics.score_coverage(self.conn, d)
        for bad in coverage_bad_days:
            self.conn.execute("UPDATE coverage SET verdict='outage' WHERE day=?",
                              ((self.today - timedelta(days=bad)).isoformat(),))
        self.conn.commit()

    def test_opens_with_correct_start_date(self):
        self._fill(silent_for=11)
        self.susp.detect(self.conn, self.today)
        rep = self.susp.report(self.conn)
        station = [e for e in rep["active"] if e["scope"] == "station"
                   and e["carrier"] == "GFA"]
        self.assertTrue(station, "station-level stop was not opened")
        e = station[0]
        # last operating day was today-11 -> the stop starts the day after
        self.assertEqual(e["last_flight_on"],
                         (self.today - timedelta(days=11)).isoformat())
        self.assertEqual(e["started_on"],
                         (self.today - timedelta(days=10)).isoformat())
        self.assertEqual(e["days_stopped"], 10)
        self.assertEqual(e["status"], "active")

    def test_resumption_never_predates_the_stop(self):
        # Event state and traffic can drift apart: a rebuilt daily_route, a
        # restored db, a re-seeded demo. The resume branch then finds no flight
        # on or after started_on and falls back to today, which used to write
        # resumed_on before started_on and a negative days_stopped.
        self._fill()                       # GFA flying every day, including today
        future = (self.today + timedelta(days=5)).isoformat()
        self.conn.execute(
            """INSERT INTO suspension (scope, scope_key, carrier, detail,
                   baseline_weekly, last_flight_on, started_on, detected_on,
                   days_stopped, status, confidence)
               VALUES ('station','GFA|OMDB','GFA','OMDB',21,NULL,?,?,0,
                       'active','observed')""",
            (future, future))
        self.conn.commit()

        self.susp.detect(self.conn, self.today)

        row = self.conn.execute(
            "SELECT resumed_on, days_stopped, status FROM suspension "
            "WHERE scope='station' AND scope_key='GFA|OMDB'").fetchone()
        self.assertEqual(row["status"], "resumed")
        self.assertGreaterEqual(row["resumed_on"], future)
        self.assertGreaterEqual(row["days_stopped"], 0)

    def test_does_not_open_below_threshold(self):
        self._fill(silent_for=4)   # route threshold is 7 days
        self.susp.detect(self.conn, self.today)
        self.assertEqual(self.susp.report(self.conn)["active"], [])

    def test_resumption_closes_the_event_with_a_duration(self):
        self._fill(silent_for=11)
        self.susp.detect(self.conn, self.today)
        # GFA flies again today
        db.upsert_flights(self.conn, [{
            "icao24": "d99999", "first_seen": 1700999999, "last_seen": None,
            "callsign": "GFA1", "carrier": "GFA", "flight_number": 1,
            "dep_icao": "OBBI", "arr_icao": "OMDB", "is_freight": 0,
            "dep_date": self.today.isoformat(), "source": "t", "ingested_at": 0}])
        metrics.rebuild_daily(self.conn)
        metrics.score_coverage(self.conn, self.today)
        self.susp.detect(self.conn, self.today)

        rep = self.susp.report(self.conn)
        self.assertEqual(rep["active"], [])
        back = [e for e in rep["recently_resumed"] if e["scope"] == "station"]
        self.assertTrue(back)
        self.assertEqual(back[0]["resumed_on"], self.today.isoformat())
        self.assertEqual(back[0]["days_stopped"], 10)

    def test_coverage_outage_days_do_not_manufacture_a_stop(self):
        """A week of dead receivers must not become 'they stopped on the 14th'."""
        self._fill(silent_for=None,
                   coverage_bad_days=range(0, 9))
        # wipe GFA rows for those days to simulate the sensor blackout
        for off in range(0, 9):
            d = (self.today - timedelta(days=off)).isoformat()
            self.conn.execute("DELETE FROM flight WHERE carrier='GFA' AND dep_date=?", (d,))
            self.conn.execute("DELETE FROM daily_route WHERE carrier='GFA' AND day=?", (d,))
        self.conn.commit()
        # today's coverage is bad, so the detector must refuse to run at all
        out = self.susp.detect(self.conn, self.today)
        self.assertTrue(out["skipped"])
        self.assertEqual(self.susp.report(self.conn)["active"], [])

    def test_no_baseline_means_no_stop(self):
        self._fill(silent_for=20)
        self.conn.execute("DELETE FROM baseline")
        self.conn.commit()
        self.susp.detect(self.conn, self.today)
        self.assertEqual(self.susp.report(self.conn)["active"], [])


class TestOpenSkyWindowing(unittest.TestCase):
    """OpenSky partitions the airport endpoints by UTC day and allows two.

    The cap is not a duration, so splitting on elapsed time is what fails: a
    48h window is legal at midnight and a 400 at every other hour. Requests
    must be cut on UTC midnight instead. Getting this wrong collected nothing
    at all -- every backfill chunk and every default 48h ingest was rejected.
    """

    def _chunks(self, begin, end):
        from src.opensky import DAY, PARTITION_DAYS
        out, cur = [], begin
        while cur < end:
            stop = min(cur - (cur % DAY) + PARTITION_DAYS * DAY, end)
            out.append((cur, stop))
            cur = stop
        return out

    def _utc_days(self, a, b):
        from src.opensky import DAY
        edges = list(range(a, b, DAY)) + [b - 1]
        return {datetime.utcfromtimestamp(t).date() for t in edges}

    def test_no_chunk_spans_more_than_two_utc_days(self):
        from src.opensky import DAY
        # Start on an awkward offset (13:47 UTC) so chunks never align by luck,
        # and cover a full baseline window.
        base = (1785000000 // DAY) * DAY + 13 * 3600 + 47 * 60
        for span_days in (1, 2, 3, 7, 92):
            end = base + span_days * DAY
            chunks = self._chunks(base, end)
            self.assertTrue(chunks, f"{span_days}d produced no chunks")
            for a, b in chunks:
                self.assertLessEqual(
                    len(self._utc_days(a, b)), 2,
                    f"{span_days}d: chunk {a}..{b} spans 3+ UTC partitions")

    def test_chunks_tile_the_range_without_gaps_or_overlap(self):
        from src.opensky import DAY
        base = (1785000000 // DAY) * DAY + 13 * 3600 + 47 * 60
        end = base + 7 * DAY
        chunks = self._chunks(base, end)
        self.assertEqual(chunks[0][0], base)
        self.assertEqual(chunks[-1][1], end)
        for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
            self.assertEqual(prev_end, next_start)


class TestNotify(unittest.TestCase):
    EVENTS = {
        "opened_events": [{"scope": "station", "carrier": "MEA", "detail": "OMDB",
                           "started_on": "2026-07-15", "days_stopped": 20,
                           "baseline_weekly": 5.2}],
        "resumed_events": [{"scope": "route", "carrier": "KAC", "detail": "OKBK-OBBI",
                            "resumed_on": "2026-08-01", "days_stopped": 14}],
    }

    def test_message_names_carrier_scope_and_dates(self):
        msg = notify.format_message(self.EVENTS, "2026-08-04")
        for token in ("MEA", "OMDB", "2026-07-15", "20d", "5.2",
                      "KAC", "OKBK-OBBI", "14d", "STOPPED", "RESUMED"):
            self.assertIn(token, msg)

    def test_long_list_is_truncated(self):
        many = {"opened_events": [dict(self.EVENTS["opened_events"][0]) for _ in range(30)],
                "resumed_events": []}
        msg = notify.format_message(many, "2026-08-04")
        self.assertIn(f"...and {30 - notify.MAX_LINES} more", msg)
        self.assertLess(len(msg), 4096)

    def test_unconfigured_is_a_silent_no_op(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("src.notify.requests.post") as post:
            self.assertFalse(notify.send("hi"))
            post.assert_not_called()

    def test_nothing_happened_sends_nothing(self):
        with mock.patch("src.notify.send") as send:
            self.assertFalse(notify.announce(
                {"opened_events": [], "resumed_events": []}, "2026-08-04"))
            send.assert_not_called()

    def test_telegram_outage_does_not_break_the_run(self):
        import requests as _rq
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("src.notify.requests.post",
                           side_effect=_rq.RequestException("boom")):
            self.assertFalse(notify.announce(self.EVENTS, "2026-08-04"))


class TestReportAttribution(unittest.TestCase):
    """A headline gets attributed to a carrier, or the carrier reads Stopped.

    Both guards here failed against live Google News output.
    """

    def test_generic_regional_headlines_attach_to_nobody(self):
        from src.report import _aliases
        # "air" is inside "airspace"/"airlines", so first-word matching pinned
        # region-wide stories onto Air Algerie and Air China and reported them
        # stopped on evidence about nobody in particular.
        for headline in (
            "Airspace closed, airlines halt flights as US, Israel attack",
            "Middle East flight disruption: regional airlines continue suspensions",
        ):
            low = headline.lower()
            for name in ("Air Algerie", "Air China", "Qatar Airways",
                         "Middle East Airlines", "British Airways"):
                self.assertFalse(
                    any(k in low for k in _aliases(name)),
                    f"{name} wrongly matched {headline!r}")

    def test_carrier_named_in_headline_is_attributed(self):
        from src.report import _aliases
        low = "Emirates and Etihad extend Bahrain and Kuwait flight cancellations".lower()
        for name in ("Emirates", "Etihad Airways"):
            self.assertTrue(any(k in low for k in _aliases(name)), name)

    def test_suspension_the_noun_counts_as_stopped(self):
        from src.report import STOPPED
        # The stem is suspenSion, so a "suspend\w*" pattern misses every
        # "Extends Flight Suspensions" headline and reads it as a mere mention.
        self.assertTrue(STOPPED.search(
            "China Southern Extends Middle East Flight Suspensions into 2027"))
        self.assertTrue(STOPPED.search("Finnair Suspends Doha and Dubai Flights"))
        self.assertFalse(STOPPED.search(
            "Airlines resume some Middle East flights but disruption continues"))


class TestBackfillResume(unittest.TestCase):
    """OpenSky's daily allowance is far smaller than a baseline harvest, so the
    harvest must span days. These guard the two ways that goes wrong silently.
    """

    def setUp(self):
        from src import backfill
        self.bf = backfill
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.patches = [
            mock.patch("src.db.DB_PATH", self.tmp.name),
            mock.patch.dict(os.environ, {"OPENSKY_CLIENT_ID": "x",
                                         "OPENSKY_CLIENT_SECRET": "y"}),
        ]
        for p in self.patches:
            p.start()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_slices_tile_the_window_without_gaps(self):
        t0, t1 = 1762000000, 1762000000 + 30 * 86400
        sl = self.bf._slices(t0, t1)
        self.assertEqual(sl[0][0], t0)
        self.assertEqual(sl[-1][1], t1)
        for (_, prev), (nxt, _) in zip(sl, sl[1:]):
            self.assertEqual(prev, nxt)

    def test_finished_slices_are_not_refetched(self):
        """A resumed run must skip what already landed, or every day of
        harvesting spends its whole allowance re-fetching day one."""
        start, end = "2025-11-01", "2025-11-15"
        t0 = int(datetime.fromisoformat(start).replace(
            tzinfo=timezone.utc).timestamp())
        t1 = int(datetime.fromisoformat(end).replace(
            tzinfo=timezone.utc).timestamp())
        slices = self.bf._slices(t0, t1)
        for a, b in slices:
            self.conn.execute(
                "INSERT INTO backfill_progress VALUES ('OTHH',?,?,0,'t')",
                (str(a), str(b)))
        self.conn.commit()

        with mock.patch("src.opensky.OpenSky.departures") as dep, \
                mock.patch("src.opensky.OpenSky.arrivals") as arr:
            res = self.bf.harvest(start, end, ["OTHH"])
            dep.assert_not_called()
            arr.assert_not_called()
        self.assertEqual(res["remaining"], 0)

    def test_interrupted_harvest_never_freezes_a_partial_baseline(self):
        """The dangerous one. A baseline built from a fraction of the window
        understates the routes it reached and omits the rest, and every
        published number is measured against it."""
        from src.opensky import RateLimited
        with mock.patch("src.opensky.OpenSky.departures",
                        side_effect=RateLimited(84288)), \
                mock.patch.object(self.bf, "freeze") as freeze:
            rc = self.bf.main(["--start", "2025-11-01", "--end", "2025-11-15",
                               "--airports", "OTHH"])
        freeze.assert_not_called()
        self.assertEqual(rc, 1)


class TestNewsRecency(unittest.TestCase):
    """A five-month-old suspension notice must not set today's verdict.

    Measured against live Google News: an unbounded carrier query returned
    headlines 134 to 157 days old at the top of the results, and those were
    deciding whether a carrier read Stopped today.
    """

    def _item(self, age_days, title="Saudia suspends flights"):
        when = datetime.now(timezone.utc) - timedelta(days=age_days)
        return {"title": title, "url": "http://x", "stance": "supports",
                "published": format_datetime(when)}

    def test_stale_headlines_are_dropped(self):
        from src import report
        with mock.patch("src.report._news", return_value=[
                self._item(150), self._item(200)]):
            self.assertEqual(report.carrier_news("Saudia"), [])

    def test_recent_headlines_are_kept_newest_first(self):
        from src import report
        with mock.patch("src.report._news", return_value=[
                self._item(20), self._item(2), self._item(150)]):
            got = report.carrier_news("Saudia")
        self.assertEqual([g["age_days"] for g in got], [2, 20])

    def test_query_bounds_recency_at_the_source(self):
        from src import report
        with mock.patch("src.report._news", return_value=[]) as news:
            report.carrier_news("Saudia", max_age=30)
        self.assertIn("when:30d", news.call_args[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
