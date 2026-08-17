"""Tests for the logic that fails silently if it is wrong.

Run: python -m pytest tests -q   (or: python tests/test_core.py)
"""
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, db, metrics  # noqa: E402
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
            "INSERT INTO baseline VALUES ('GFA','OMDB','OTHH',21,28,?,?)",
            ((today - timedelta(days=28)).isoformat(), today.isoformat()))
        self.conn.commit()

        # Score every day in the window, not just today. Each ingest scores its
        # own reference day, so in the live pipeline these rows accumulate --
        # and silent_days() now needs them: a day with no coverage row is a day
        # nobody looked at, and those cannot add up to proven silence.
        for off in range(29):
            metrics.score_coverage(self.conn, today - timedelta(days=off))
        self.conn.commit()

        cov = metrics.score_coverage(self.conn, today)
        self.assertEqual(cov["verdict"], "ok")
        report = metrics.route_report(self.conn, today)
        gfa = [r for r in report["routes"] if r["carrier"] == "GFA"][0]
        self.assertEqual(gfa["status"], "SUSPENDED")
        self.assertGreaterEqual(gfa["silent_days"], config.SUSPENSION_CONFIRM_DAYS)

    def test_blind_days_do_not_confirm_a_suspension(self):
        """The 2026-08-12 shape: a long outage stretch behind one quiet day.

        GFA's last flight was 3 days ago, but only one of the days since then
        was observed -- the rest were `outage`, which is what the whole
        backfill week looked like. One observed silent day is not three.
        """
        today = date.today()
        rows, i = [], 0
        for off in range(28, -1, -1):
            d = today - timedelta(days=off)
            for _ in range(10):
                i += 1
                rows.append(self._leg("UAE", d, i))
            if off > 2:
                for _ in range(3):
                    i += 1
                    rows.append(self._leg("GFA", d, i))
        db.upsert_flights(self.conn, rows)
        metrics.rebuild_daily(self.conn)
        self.conn.execute(
            "INSERT INTO baseline VALUES ('GFA','OMDB','OTHH',21,28,?,?)",
            ((today - timedelta(days=28)).isoformat(), today.isoformat()))
        for off in range(29):
            metrics.score_coverage(self.conn, today - timedelta(days=off))
        # The two days before today were blind, exactly as 2026-08-04..10 were.
        for off in (1, 2):
            self.conn.execute("UPDATE coverage SET verdict='outage' WHERE day=?",
                              ((today - timedelta(days=off)).isoformat(),))
        self.conn.commit()

        quiet = metrics.silent_days(self.conn, ("GFA", "OMDB", "OTHH"), today)
        self.assertEqual(
            quiet, 1,
            "blind days were counted as silence -- that is what published ten "
            "carriers as SUSPENDED on 2026-08-12")
        report = metrics.route_report(self.conn, today)
        gfa = [r for r in report["routes"] if r["carrier"] == "GFA"][0]
        self.assertNotEqual(
            gfa["status"], "SUSPENDED",
            "one observed silent day must not confirm a suspension")


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


class _Clock:
    """A fake clock that never advances on its own, so a burst stays a burst."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestBackendPacing(unittest.TestCase):
    """The spacing that keeps adsb.lol from 429ing halfway through a sweep.

    This is the failure that does not look like one: when both backends are
    exhausted _get() returns an empty list, firwatch records a FIR with no
    carriers, and that reads exactly like empty airspace.
    """

    def setUp(self):
        # Reload rather than reach into module state, so the test asserts on
        # timing alone and would still fail -- on the timing -- against any
        # implementation that lets the burst through.
        import importlib
        from src import adsb_live
        self.adsb = importlib.reload(adsb_live)
        self.clock = _Clock()
        self.time_patch = mock.patch.object(self.adsb, "time", self.clock)
        self.time_patch.start()

    def tearDown(self):
        self.time_patch.stop()

    def _serve(self, responses):
        """Answer each request from `responses`, keyed by backend name.

        A backend the test says nothing about answers 503, so adding one to
        BACKENDS does not silently change what a test is asserting.
        """
        calls = []

        def fake_get(url, **kw):
            name = next(n for n, base, _ in self.adsb.BACKENDS if url.startswith(base))
            calls.append((self.clock.now, name))
            return responses.get(name, _Resp(503))

        return calls, mock.patch.object(self.adsb._session, "get", fake_get)

    def test_successive_calls_to_one_backend_are_spaced(self):
        ok = {"adsblol": _Resp(200, {"ac": [{"hex": "abc"}]})}
        calls, patch = self._serve(ok)
        with patch:
            for _ in range(4):
                backend, ac = self.adsb.point(25.25, 55.36, 250)
                self.assertEqual(backend, "adsblol")
                self.assertEqual(len(ac), 1)

        # One request a second is what airplanes.live asks for in writing and
        # what adsb.lol throttles below. Asserted as that contract rather than
        # against whatever constant the module happens to name, so this fails
        # on the timing for any implementation that lets the burst through.
        one_per_second = 1.0
        gaps = [b[0] - a[0] for a, b in zip(calls, calls[1:])]
        self.assertEqual(len(calls), 4)
        for gap in gaps:
            self.assertGreaterEqual(
                gap, one_per_second,
                f"calls {gap:.3f}s apart -- that is the burst that got adsb.lol "
                "to 429 on 6 of 12 FIR points on 2026-08-10")

    def test_first_call_does_not_wait(self):
        calls, patch = self._serve({"adsblol": _Resp(200, {"ac": []})})
        with patch:
            self.adsb.point(25.25, 55.36, 250)
        self.assertEqual(self.clock.slept, [])

    def test_fallback_is_not_delayed_by_the_pacing(self):
        # Each backend is paced on its own clock, so falling through to a
        # backend we have not called costs nothing. The old code slept 1.1s
        # here for no reason.
        calls, patch = self._serve({
            "adsblol": _Resp(429),
            "airplaneslive": _Resp(200, {"ac": [{"hex": "abc"}]}),
        })
        with patch:
            backend, ac = self.adsb.point(25.25, 55.36, 250)
        self.assertEqual(backend, "airplaneslive")
        self.assertEqual(len(ac), 1)
        self.assertEqual([n for _, n in calls], ["adsblol", "airplaneslive"])
        self.assertEqual(self.clock.slept, [])

    def test_all_backends_down_reports_nothing_not_empty_airspace(self):
        calls, patch = self._serve({
            "adsblol": _Resp(429), "airplaneslive": _Resp(503),
        })
        with patch:
            backend, ac = self.adsb.point(25.25, 55.36, 250)
        # firwatch keys its log line off this: "none" is the tell that the
        # zero below it is a source failure and not empty sky.
        self.assertEqual(backend, "none")
        self.assertEqual(ac, [])




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
            "INSERT INTO baseline VALUES ('GFA','OBBI','OMDB',21,28,?,?)",
            ((self.today - timedelta(days=28)).isoformat(), self.today.isoformat()))
        for off in range(29):
            d = self.today - timedelta(days=off)
            metrics.score_coverage(self.conn, d)
        for bad in coverage_bad_days:
            self.conn.execute("UPDATE coverage SET verdict='outage' WHERE day=?",
                              ((self.today - timedelta(days=bad)).isoformat(),))
        self.conn.commit()

    def test_a_day_nobody_looked_at_is_not_a_silent_day(self):
        """The 2026-08-12 blow-up: 270 stops opened out of a hole in the data.

        The baseline harvest ends 2026-01-31 and observation began 2026-08-01.
        Every day in between has no coverage row because no run ever covered
        it. Defaulting those to "ok" made ~190 unobserved days read as ~190
        days of proven silence, which is past every threshold, so whole
        carriers were published as STOPPED since January at confidence
        `observed`.
        """
        from src.suspensions import silence
        today = date(2026, 8, 12)
        last_seen = date(2026, 1, 31)

        # Flew every day of the baseline window, and it was observed.
        counts = {(last_seen - timedelta(days=i)).isoformat(): 3
                  for i in range(30)}
        cov = {d: "ok" for d in counts}
        # Then a six-month hole nobody looked at -- no coverage rows at all.
        # Then three observed days this month with no flight on them.
        for i in range(3):
            cov[(today - timedelta(days=i)).isoformat()] = "ok"

        s = silence(counts, cov, today)
        self.assertEqual(
            s["silent_days"], 3,
            "unobserved days were counted as silence -- that is the bug that "
            "published 270 suspensions off a gap in the calendar")
        self.assertEqual(s["last_flight_on"], last_seen.isoformat())
        # The hole is reported as what it is, rather than as evidence.
        self.assertGreater(s["coverage_days_skipped"], 150)

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

    def test_scattered_observed_days_are_not_a_week_of_silence(self):
        """The 2026-08-17 blow-up: 147 route stops off seven glimpses.

        Coverage gating counts observed days only, which is right, but seven
        observed silent days can be spread over any span when coverage is
        sparse. The real record that morning was 08-01/02/03, 08-10/11,
        08-15/16 -- seven ok days across sixteen calendar days -- and every
        route hit the seven-day route threshold on it. Seven must fit inside
        SPAN_DAYS["route"] before it means a week.
        """
        from src.suspensions import silence, SPAN_DAYS

        day = date(2026, 8, 16)
        ok = ["2026-08-01", "2026-08-02", "2026-08-03",
              "2026-08-10", "2026-08-11", "2026-08-15", "2026-08-16"]
        cov = {d: "ok" for d in ok}
        # Flew, and was seen flying, well before the observation window opened.
        counts = {"2026-07-20": 3}
        cov["2026-07-20"] = "ok"

        spread = silence(counts, cov, day, SPAN_DAYS["route"])
        self.assertLess(
            spread["silent_days"], self.susp.THRESHOLD["route"],
            "seven observed days spread over sixteen calendar days were "
            "counted as a week of silence -- that is what opened 147 stops")

        # Same seven days, actually consecutive: that IS a week of silence.
        tight = {(day - timedelta(days=i)).isoformat(): "ok" for i in range(7)}
        tight["2026-07-20"] = "ok"
        self.assertGreaterEqual(
            silence(counts, tight, day, SPAN_DAYS["route"])["silent_days"],
            self.susp.THRESHOLD["route"])

    def test_never_seen_flying_is_not_a_stop(self):
        """No sighting, no stop. Absent from our receivers is not absent.

        British Airways held five of the 147, London-Dubai among them at 18
        departures a week, on zero observed legs in the whole record. The old
        code dated those from `day - silent_days`, subtracting observed days as
        if they were calendar days, and published a start date whose own
        coverage row read `outage`.
        """
        self._fill(silent_for=None)
        # A baseline route we have never once seen operate.
        self.conn.execute(
            "INSERT INTO baseline VALUES ('BAW','EGLL','OMDB',18,28,?,?)",
            ((self.today - timedelta(days=28)).isoformat(),
             self.today.isoformat()))
        self.conn.commit()

        self.susp.detect(self.conn, self.today)

        baw = [e for e in self.susp.report(self.conn)["active"]
               if e["carrier"] == "BAW"]
        self.assertEqual(
            baw, [],
            "a route never seen flying was published as stopped; there is no "
            "day to date the stop from")

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


class TestFlightBoardGuard(unittest.TestCase):
    """The board endpoint is undocumented. When it breaks it will not error,
    it will return an empty list -- which without a guard reads as "every
    carrier stopped serving Baghdad overnight". These pin that it cannot.
    """

    def setUp(self):
        from src import flightboard
        self.fb = flightboard
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.patches = [mock.patch("src.db.DB_PATH", self.tmp.name),
                        mock.patch("src.flightboard.time.sleep")]
        for p in self.patches:
            p.start()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def _history(self, counts, airport="ORBI"):
        for i, n in enumerate(counts):
            day = f"2026-07-{10 + i:02d}"
            self.conn.execute(
                "INSERT INTO board_probe (airport, day, flights, verdict) "
                "VALUES (?,?,?,?)", (airport, day, n, "ok"))
        self.conn.commit()

    def test_a_sudden_zero_is_the_source_failing_not_an_empty_sky(self):
        self._history([50, 48, 52, 49])
        verdict, median = self.fb._verdict(self.conn, "ORBI", "2026-07-20", 0)
        self.assertEqual(verdict, "empty")
        self.assertEqual(median, 49.5)

    def test_a_collapse_short_of_zero_is_also_flagged(self):
        self._history([50, 48, 52, 49])
        verdict, _ = self.fb._verdict(self.conn, "ORBI", "2026-07-20", 4)
        self.assertEqual(verdict, "thin")

    def test_a_real_drop_that_is_not_a_collapse_still_reads_ok(self):
        self._history([50, 48, 52, 49])
        verdict, _ = self.fb._verdict(self.conn, "ORBI", "2026-07-20", 30)
        self.assertEqual(verdict, "ok")

    def test_zero_means_nothing_before_there_is_history_to_judge_it_by(self):
        self._history([50, 48])          # under MIN_HISTORY_DAYS
        verdict, _ = self.fb._verdict(self.conn, "ORBI", "2026-07-20", 0)
        self.assertEqual(verdict, "unproven")

    def test_a_total_request_failure_records_no_count_at_all(self):
        """A zero written on a network failure would poison the median that
        every later day is judged against."""
        with mock.patch.object(self.fb, "_fetch", return_value=None):
            out = self.fb.sample(self.conn)
        rows = self.conn.execute("SELECT COUNT(*) n FROM board_probe").fetchone()["n"]
        self.assertEqual(rows, 0)
        self.assertEqual(out["written"], 0)
        self.assertTrue(all(f.endswith(":failed") for f in out["flagged"]))

    def test_board_rows_never_land_in_the_flight_table(self):
        """A board listing is not a sighting; merging the two would let the
        coverage score and stop detection treat a timetable as observation."""
        flights = [{"carrier": {"fs": "EK", "name": "Emirates", "flightNumber": "941"},
                    "airport": {"fs": "DXB"}, "arrivalTime": {"time24": "11:15"}}]
        with mock.patch.object(self.fb, "_fetch", return_value=flights):
            self.fb.sample(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM flight").fetchone()["n"], 0)
        self.assertGreater(
            self.conn.execute("SELECT COUNT(*) n FROM board_flight").fetchone()["n"], 0)


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
        # Not 1: an unhandled exception exits 1 too, and the workflow has to
        # tell "allowance ran out, commit what landed" from "this broke".
        self.assertEqual(rc, self.bf.EXIT_INCOMPLETE)


class TestCompaction(unittest.TestCase):
    """The harvested window is kept as rollups and its raw legs dropped, so
    the committed database stops growing without bound. The baseline must come
    out identical either way -- otherwise compaction quietly rewrites history.
    """

    def setUp(self):
        from src import backfill
        self.bf = backfill
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.patch = mock.patch("src.db.DB_PATH", self.tmp.name)
        self.patch.start()
        self.conn = db.connect(self.tmp.name)
        # Two routes over two weeks, plus freight that must never count.
        rows = []
        for i in range(14):
            day = (date(2025, 11, 1) + timedelta(days=i)).isoformat()
            for j in range(3):
                rows.append({"icao24": f"a{i}{j}", "first_seen": 1762000000 + i * 86400 + j,
                             "last_seen": None, "callsign": f"QTR{j}", "carrier": "QTR",
                             "flight_number": j, "dep_icao": "OTHH", "arr_icao": "OMDB",
                             "is_freight": 0, "dep_date": day, "source": "opensky",
                             "ingested_at": 0})
            rows.append({"icao24": f"f{i}", "first_seen": 1762000000 + i * 86400 + 9,
                         "last_seen": None, "callsign": "QTR9", "carrier": "QTR",
                         "flight_number": 9, "dep_icao": "OTHH", "arr_icao": "OMDB",
                         "is_freight": 1, "dep_date": day, "source": "opensky",
                         "ingested_at": 0})
        db.upsert_flights(self.conn, rows)
        self.conn.commit()

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def _freeze_rows(self):
        return {(r["carrier"], r["dep_icao"], r["arr_icao"]):
                (r["weekly_freq"], r["sample_days"])
                for r in self.conn.execute("SELECT * FROM baseline")}

    def test_baseline_is_identical_before_and_after_compaction(self):
        from src import metrics
        metrics.rebuild_daily(self.conn, since="2025-11-01", until="2025-11-14")
        self.bf.freeze("2025-11-01", "2025-11-14")
        before = self._freeze_rows()

        self.bf.compact("2025-11-01", "2025-11-14")
        self.bf.freeze("2025-11-01", "2025-11-14")
        self.assertEqual(self._freeze_rows(), before)
        self.assertEqual(before[("QTR", "OTHH", "OMDB")][1], 14)   # freight excluded

    def test_compaction_drops_the_raw_legs_it_rolled_up(self):
        self.bf.compact("2025-11-01", "2025-11-14")
        left = self.conn.execute("SELECT COUNT(*) n FROM flight").fetchone()["n"]
        kept = self.conn.execute("SELECT COUNT(*) n FROM daily_route").fetchone()["n"]
        self.assertEqual(left, 0)
        self.assertEqual(kept, 14)

    def test_recent_days_are_never_pruned(self):
        """Coverage scoring reads a 28-day median straight off the raw legs;
        pruning into that window would score the network as blind."""
        today = date.today()
        recent = [{"icao24": "r1", "first_seen": 1, "last_seen": None,
                   "callsign": "UAE1", "carrier": "UAE", "flight_number": 1,
                   "dep_icao": "OMDB", "arr_icao": "OTHH", "is_freight": 0,
                   "dep_date": today.isoformat(), "source": "opensky",
                   "ingested_at": 0}]
        db.upsert_flights(self.conn, recent)
        self.conn.commit()
        out = self.bf.compact("2025-11-01", today.isoformat())
        self.assertEqual(out["pruned"], 0)
        self.assertTrue(self.conn.execute(
            "SELECT COUNT(*) n FROM flight").fetchone()["n"])

    def test_a_later_ingest_rebuild_does_not_erase_compacted_days(self):
        """ingest rebuilds daily_route over a trailing window. If that reached
        back over pruned days it would delete rollups it cannot regenerate."""
        from src import metrics
        self.bf.compact("2025-11-01", "2025-11-14")
        metrics.rebuild_daily(self.conn, since=date.today().isoformat())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM daily_route").fetchone()["n"], 14)

    def test_the_default_airport_set_skips_the_blind_ones(self):
        """A scheduled run passes no inputs at all, so this default is what a
        cron harvest actually uses. Getting it wrong spends half the daily
        allowance on airports measured to return zero."""
        from src import schedules
        picked = self.bf.observable_airports()
        self.assertEqual(len(picked), 8)
        iata = {config.airports()[i]["iata"] for i in picked}
        self.assertFalse(iata & set(schedules.BLIND))

    def test_compacting_twice_does_not_destroy_the_rollup(self):
        """A second run over a finished window finds no raw legs. Rebuilding
        the range from them would clear the rollup and regenerate nothing,
        leaving freeze() with an empty baseline."""
        self.bf.compact("2025-11-01", "2025-11-14")
        first = self.conn.execute(
            "SELECT COUNT(*) n FROM daily_route").fetchone()["n"]
        out = self.bf.compact("2025-11-01", "2025-11-14")
        self.assertEqual(out, {"rolled_up": 0, "pruned": 0})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM daily_route").fetchone()["n"], first)
        self.bf.freeze("2025-11-01", "2025-11-14")
        self.assertTrue(self._freeze_rows())

    def test_an_incomplete_harvest_is_never_compacted(self):
        """Pruning between runs would break the dedup the flight primary key
        gives us: these airports fly to each other, so the same leg arrives
        from two airports' fetches and collapses onto one row only while both
        copies are still rows. Fold the first away and the second is counted
        twice."""
        with mock.patch.object(self.bf, "harvest",
                               return_value={"legs": 0, "done": 1, "remaining": 7}), \
                mock.patch.object(self.bf, "compact") as compact, \
                mock.patch.object(self.bf, "freeze") as freeze:
            rc = self.bf.main(["--start", "2025-11-01", "--end", "2025-11-14"])
        compact.assert_not_called()
        freeze.assert_not_called()
        self.assertEqual(rc, self.bf.EXIT_INCOMPLETE)

    def test_a_complete_harvest_compacts_before_it_freezes(self):
        """Reversed, freeze() would read a rollup that has not been written
        for the days whose legs are about to be dropped."""
        calls = []
        with mock.patch.object(self.bf, "harvest",
                               return_value={"legs": 9, "done": 8, "remaining": 0}), \
                mock.patch.object(self.bf, "compact",
                                  side_effect=lambda *a: calls.append("compact")), \
                mock.patch.object(self.bf, "freeze",
                                  side_effect=lambda *a: calls.append("freeze")):
            rc = self.bf.main(["--start", "2025-11-01", "--end", "2025-11-14"])
        self.assertEqual(calls, ["compact", "freeze"])
        self.assertEqual(rc, 0)


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


class TestNewsSourceFailure(unittest.TestCase):
    """A source that did not answer must never read as a source that found
    nothing.

    Measured 2026-08-06: every Google News query from the Actions runner
    returned 503 while the same queries worked elsewhere. The report logged
    "0 headlines" for each and dropped the whole press section, so the page
    said nothing was reported about Kuwait when nothing had been asked.
    """

    def _item(self, title="Kuwait airport halts flights"):
        when = datetime.now(timezone.utc) - timedelta(days=2)
        return {"title": title, "url": "http://x", "stance": "supports",
                "published": format_datetime(when)}

    def test_a_failed_lookup_is_none_not_an_empty_list(self):
        from src import corroborate
        with mock.patch.object(corroborate._session, "get",
                               side_effect=corroborate.requests.RequestException("503")):
            self.assertIsNone(corroborate._news("anything"))

    def test_carrier_lookup_passes_the_failure_up(self):
        from src import report
        with mock.patch("src.report._news", return_value=None):
            self.assertIsNone(report.carrier_news("Saudia"))
        with mock.patch("src.report._news", return_value=[]):
            self.assertEqual(report.carrier_news("Saudia"), [])

    def test_verdict_says_the_press_was_not_asked(self):
        from src import report
        _, gerekce = report.verdict({}, None, None)
        self.assertIn("yanıt vermedi", gerekce)
        _, bulunamadi = report.verdict({}, None, [])
        self.assertNotIn("yanıt vermedi", bulunamadi)

    def test_a_headline_from_another_continent_cannot_stop_a_carrier(self):
        """The 2026-08-09 report's only STOPPED verdict, in full: Air China was
        published as having stopped because "Air China launches regular flights
        on the Beijing - Bishkek - Beijing route" came back classified
        `stopped`. Neither seen nor timetabled here, one label was the whole
        case."""
        from src import report
        haber = [{"signal": "stopped", "airports": [],
                  "title": "Air China launches regular flights on the "
                           "Beijing - Bishkek - Beijing route - open.kg"}]
        durum, _ = report.verdict({}, None, haber)
        self.assertEqual(durum, "unknown")

    def test_an_it_outage_is_not_a_resumption_either(self):
        from src import report
        haber = [{"signal": "resumed", "airports": [],
                  "title": "American Airlines resumes flights after IT issue "
                           "forced brief nationwide halt - Reuters"}]
        _, gerekce = report.verdict({}, None, haber)
        self.assertNotIn("çelişki", gerekce)

    def test_a_headline_naming_a_watched_place_still_counts(self):
        from src import report
        for haber in (
            # the classifier tied it to our airports
            [{"signal": "stopped", "airports": ["BAH", "KWI"],
              "title": "Emirates flights remain suspended"}],
            # no classifier answer, but the headline names the place itself
            [{"signal": "stopped", "airports": [],
              "title": "5 airlines suspend flights to and from Riyadh"}],
            [{"signal": "stopped", "airports": [],
              "title": "China Southern extends Middle East flight suspensions"}],
        ):
            durum, _ = report.verdict({"OMDB": 4}, None, haber)
            self.assertEqual(durum, "partial", haber[0]["title"])

    def test_the_publisher_name_is_not_evidence_of_where(self):
        """Google appends " - Publisher" to every title, and the publishers are
        Gulf News and timeoutriyadh.com."""
        from src import report
        haber = [{"signal": "stopped", "airports": [],
                  "title": "Airline cuts two long-haul routes until 2027 "
                           "- Gulf News"}]
        durum, _ = report.verdict({"OMDB": 4}, None, haber)
        self.assertEqual(durum, "flying")

    def test_blind_airport_failure_is_flagged_not_silently_empty(self):
        from src import report
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        conn = db.connect(tmp.name)
        try:
            with mock.patch("src.report._news", return_value=None):
                blocks = report.blind_news(conn)
            self.assertTrue(blocks)
            self.assertTrue(all(b["failed"] and not b["items"] for b in blocks))
        finally:
            conn.close()
            os.unlink(tmp.name)

    def test_the_press_section_survives_a_dead_source(self):
        """Returning "" here is what hid the outage on 2026-08-06."""
        from src import report
        html = report._blind_news_section(
            {"blind_news": [{"iata": "KWI", "city": "Kuwait",
                             "items": [], "failed": True}]})
        self.assertIn("Kör havalimanları", html)
        self.assertIn("sorulamadığı", html)

    def test_no_section_when_there_is_genuinely_no_news(self):
        from src import report
        html = report._blind_news_section(
            {"blind_news": [{"iata": "KWI", "city": "Kuwait",
                             "items": [], "failed": False}]})
        self.assertEqual(html, "")


class TestHeadlineCache(unittest.TestCase):
    """When the press source is down the report shows the last set it did
    answer with, dated. That is a reading aid, never evidence: a headline that
    could not be re-checked today must not be able to move a carrier's state.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _item(self, title="Kuwait airport halts flights", age=1):
        when = datetime.now(timezone.utc) - timedelta(days=age)
        return {"title": title, "url": "http://x", "published": format_datetime(when),
                "stance": "supports", "age_days": age, "signal": "stopped"}

    def test_a_good_run_is_remembered_and_a_dead_one_recalls_it(self):
        from src import report
        report._remember(self.conn, "airport", "KWI", [self._item()])
        got = report._recall(self.conn, "airport", "KWI")
        self.assertEqual(len(got["items"]), 1)
        self.assertEqual(got["age_days"], 0)

    def test_nothing_to_recall_reads_as_nothing(self):
        from src import report
        self.assertIsNone(report._recall(self.conn, "carrier", "QTR"))
        report._remember(self.conn, "carrier", "QTR", [])
        self.assertIsNone(report._recall(self.conn, "carrier", "QTR"))

    def test_a_cached_stop_headline_never_sets_the_state(self):
        """The whole point. verdict() sees None, not the cached items."""
        from src import report
        report._remember(self.conn, "carrier", "SVA", [self._item("Saudia suspends")])
        state, why = report.verdict({}, None, None)
        self.assertEqual(state, "unknown")
        self.assertIn("yanıt vermedi", why)

    def test_the_newest_good_answer_replaces_the_older_one(self):
        from src import report
        report._remember(self.conn, "airport", "KWI", [self._item("old")])
        report._remember(self.conn, "airport", "KWI",
                         [self._item("new"), self._item("newer")])
        got = report._recall(self.conn, "airport", "KWI")
        self.assertEqual([i["title"] for i in got["items"]], ["new", "newer"])

    def test_cached_items_render_marked_as_stale(self):
        from src import report
        html = report._blind_news_section({"blind_news": [
            {"iata": "KWI", "city": "Kuwait", "items": [], "failed": True,
             "stale": {"items": [self._item()], "age_days": 3}}]})
        self.assertIn("arşiv", html)
        self.assertIn("önce alındı", html)
        self.assertIn("bugün doğrulanmadılar", html)


class TestHeadlineClassification(unittest.TestCase):
    """The model reads headlines. It must never be able to invent geography,
    and it must never make the report irreproducible."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _reply(self, payload):
        r = mock.Mock()
        r.status_code = 200
        r.raise_for_status = mock.Mock()
        r.json.return_value = {"choices": [{"message": {
            "content": __import__("json").dumps(payload)}}]}
        return r

    def test_invented_airport_codes_are_dropped(self):
        from src import classify
        # Asked for free-form codes the model answered "KUW" for Kuwait, which
        # is not a real IATA code. Anything outside the configured set goes.
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}), \
                mock.patch("src.classify.requests.post", return_value=self._reply(
                    {"action": "stopped", "airports": ["BAH", "KUW", "XXX"],
                     "why": "cancellations"})):
            got = classify.classify(self.conn, "UAE", "Emirates",
                                    {"url": "u1", "title": "t"})
        self.assertEqual(got["airports"], ["BAH"])

    def test_unknown_action_falls_back_to_regex(self):
        from src import classify
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}), \
                mock.patch("src.classify.requests.post", return_value=self._reply(
                    {"action": "banana", "airports": []})):
            self.assertIsNone(classify.classify(
                self.conn, "UAE", "Emirates", {"url": "u2", "title": "t"}))

    def test_second_run_is_cached_not_re_asked(self):
        """An uncached call makes the same report give different answers on
        the same data, which is the one property this project cannot trade."""
        from src import classify
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}), \
                mock.patch("src.classify.requests.post", return_value=self._reply(
                    {"action": "resumed", "airports": [], "why": "back"})) as post:
            first = classify.classify(self.conn, "RJA", "Royal Jordanian",
                                      {"url": "u3", "title": "t"})
            second = classify.classify(self.conn, "RJA", "Royal Jordanian",
                                       {"url": "u3", "title": "t"})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(first["action"], second["action"])
        self.assertTrue(second["cached"])

    def test_a_prompt_change_retires_the_cached_answers(self):
        """Otherwise the run that teaches the model to say "irrelevant" keeps
        serving the answer the old prompt gave for the same headline."""
        from src import classify
        self.conn.execute(
            """INSERT INTO headline_class
               (url, carrier, action, airports, why, model, classified_at)
               VALUES ('u9','CCA','stopped','','route launch not mentioned',
                       'ministral-3b-latest','2026-08-04T18:58:32+00:00')""")
        self.conn.commit()
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}), \
                mock.patch("src.classify.requests.post") as post:
            post.return_value = self._reply(
                {"action": "irrelevant", "airports": [], "why": "route launch"})
            got = classify.classify(self.conn, "CCA", "Air China",
                                    {"url": "u9", "title": "Air China launches"})
            post.assert_called_once()
        self.assertEqual(got["action"], "irrelevant")

    def test_no_key_means_no_opinion(self):
        from src import classify
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("src.classify.config.ROOT", pathlib.Path("/nonexistent")), \
                mock.patch("src.classify.requests.post") as post:
            self.assertIsNone(classify.classify(
                self.conn, "UAE", "Emirates", {"url": "u4", "title": "t"}))
            post.assert_not_called()


class TestChangeTracking(unittest.TestCase):
    """A snapshot says where things stand; an operator needs what moved."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _rows(self, state, seen):
        return [{"code": "ETD", "name": "Etihad Airways", "state": state,
                 "legs": sum(seen.values()), "seen_at": seen}]

    def _yesterday(self, airports, verdict="ok"):
        """A previous report, and whether anyone could see on that day."""
        self.conn.execute(
            "INSERT INTO report_state VALUES ('2000-01-01','ETD','flying',9,?)",
            (airports,))
        self.conn.execute(
            "INSERT INTO coverage (day, control_flights, median_28d, score, verdict)"
            " VALUES ('2000-01-01', 300, 300.0, 1.0, ?)", (verdict,))
        self.conn.commit()

    def test_first_run_reports_nothing_and_records_state(self):
        from src import report
        out = report.diff_since_last(
            self.conn, self._rows("flying", {"OMDB": 5}), True)
        self.assertIsNone(out["since"])
        self.assertEqual(out["changes"], [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM report_state").fetchone()["n"], 1)

    def test_state_change_and_dropped_airport_are_both_reported(self):
        from src import report
        self._yesterday("OMDB,OBBI")
        out = report.diff_since_last(
            self.conn, self._rows("partial", {"OMDB": 5}), True)
        kinds = {c["kind"] for c in out["changes"]}
        self.assertIn("durum", kinds)
        self.assertIn("kayboldu", kinds)
        gone = [c for c in out["changes"] if c["kind"] == "kayboldu"][0]
        self.assertEqual(gone["was"], "BAH")     # OBBI -> its IATA code

    def test_a_returning_airport_is_reported(self):
        from src import report
        self._yesterday("OMDB")
        out = report.diff_since_last(
            self.conn, self._rows("flying", {"OMDB": 5, "OKBK": 2}), True)
        back = [c for c in out["changes"] if c["kind"] == "geri döndü"]
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["now"], "KWI")

    def test_a_blind_day_reports_no_airport_movement(self):
        """The window loses its tail when nobody looks. That is not a departure
        that stopped, and six carriers were reported as having lost airports on
        exactly such a day."""
        from src import report
        self._yesterday("OMDB,OBBI")
        out = report.diff_since_last(
            self.conn, self._rows("partial", {"OMDB": 5}), False)
        kinds = {c["kind"] for c in out["changes"]}
        self.assertIn("durum", kinds)          # the press still moves a state
        self.assertNotIn("kayboldu", kinds)
        self.assertFalse(out["airports_compared"])

    def test_a_blind_day_is_not_compared_against_afterwards_either(self):
        """Yesterday was blind, today can see: its shrunken airport list would
        read as everyone returning at once."""
        from src import report
        self._yesterday("OMDB", verdict="outage")
        out = report.diff_since_last(
            self.conn, self._rows("flying", {"OMDB": 5, "OKBK": 2}), True)
        self.assertEqual([c["kind"] for c in out["changes"]], [])
        self.assertFalse(out["airports_compared"])


class TestBlindWeekEndToEnd(unittest.TestCase):
    """The 2026-08-12 shape, run through the whole pipeline at once.

    Three separate defects shipped that day and every one of them passed its
    own unit tests. What none of them was asked was the composed question:
    given a frozen baseline, a months-long hole where nobody looked, and a week
    where the allowance was spent, does the pipeline claim anything?

    It must not. Not a suspension, not a SUSPENDED status, not an alert.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = db.connect(self.tmp.name)
        self.today = date.today()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _leg(self, carrier, day, i):
        return {
            "icao24": f"bb{i:05x}", "first_seen": 1700000000 + i * 60,
            "last_seen": None, "callsign": f"{carrier}{i}", "carrier": carrier,
            "flight_number": i, "dep_icao": "OMDB", "arr_icao": "OTHH",
            "is_freight": 0, "dep_date": day.isoformat(), "source": "t",
            "ingested_at": 0,
        }

    def _build(self):
        """A baseline window, then a 120-day hole, then a mostly-blind week."""
        from src import suspensions
        rows, i = [], 0

        # 1. The frozen reference period: everyone flying, every day observed.
        base_end = self.today - timedelta(days=127)
        for off in range(30):
            d = base_end - timedelta(days=off)
            for _ in range(10):
                i += 1
                rows.append(self._leg("UAE", d, i))
            for _ in range(3):
                i += 1
                rows.append(self._leg("GFA", d, i))

        # 2. A 120-day hole. No flights, and -- the point -- no coverage rows,
        #    because no run ever looked. This is Feb-Jul 2026.

        # 3. The last week: controls flying on two days only. The other five
        #    are the allowance-exhausted days, seen by nobody.
        observed = [self.today, self.today - timedelta(days=1)]
        for d in observed:
            for _ in range(10):
                i += 1
                rows.append(self._leg("UAE", d, i))

        db.upsert_flights(self.conn, rows)
        metrics.rebuild_daily(self.conn)
        self.conn.execute(
            "INSERT INTO baseline VALUES ('GFA','OMDB','OTHH',21,30,?,?)",
            ((base_end - timedelta(days=29)).isoformat(), base_end.isoformat()))
        self.conn.execute(
            "INSERT INTO baseline VALUES ('UAE','OMDB','OTHH',70,30,?,?)",
            ((base_end - timedelta(days=29)).isoformat(), base_end.isoformat()))

        # Score only the days a run would have scored: the baseline window and
        # the two days of the last week that produced data.
        for off in range(30):
            metrics.score_coverage(self.conn, base_end - timedelta(days=off))
        for d in observed:
            metrics.score_coverage(self.conn, d)
        self.conn.commit()
        return suspensions

    def test_a_hole_in_the_calendar_claims_nothing(self):
        suspensions = self._build()

        cov = metrics.coverage_map(self.conn)
        self.assertEqual(metrics.verdict_for(cov, (self.today - timedelta(days=60)).isoformat()),
                         metrics.NEVER_OBSERVED,
                         "a day in the hole must be named, not defaulted")

        # GFA has not flown since the baseline window -- 127 days ago -- but
        # nobody looked for 120 of them.
        events = suspensions.detect(self.conn, self.today)
        self.assertEqual(
            events["opened"], 0,
            "the hole between baseline and observation was read as evidence; "
            "this is the 270-suspension failure of 2026-08-12")

        report = metrics.route_report(self.conn, self.today)
        self.assertEqual(report["observed_days"], 2)
        self.assertFalse(report["ratios_published"])
        for r in report["routes"]:
            self.assertEqual(
                r["status"], "UNKNOWN",
                f"{r['carrier']} was scored on a window we barely saw")
            self.assertIsNone(r["ratio"])
        self.assertEqual(metrics.alerts_from(report), [])

    def test_it_starts_answering_once_we_have_looked(self):
        """The same pipeline must not be permanently mute -- observe, and it speaks."""
        suspensions = self._build()
        # Five observed days of controls flying, GFA absent throughout.
        rows, i = [], 90000
        for off in range(5):
            d = self.today - timedelta(days=off)
            for _ in range(10):
                i += 1
                rows.append(self._leg("UAE", d, i))
        db.upsert_flights(self.conn, rows)
        metrics.rebuild_daily(self.conn)
        for off in range(5):
            metrics.score_coverage(self.conn, self.today - timedelta(days=off))
        self.conn.commit()

        report = metrics.route_report(self.conn, self.today)
        self.assertGreaterEqual(report["observed_days"], config.MIN_OBSERVED_DAYS)
        self.assertTrue(report["ratios_published"])
        uae = [r for r in report["routes"] if r["carrier"] == "UAE"][0]
        self.assertEqual(uae["status"], "NORMAL",
                         "scaling five observed days to a week should land on baseline")
        gfa = [r for r in report["routes"] if r["carrier"] == "GFA"][0]
        self.assertIn(gfa["status"], ("SUSPENDED", "MINIMAL"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
