"""Unit tests of the pipeline stages:  python -m unittest discover -s tests -v"""
import json
import math
import unittest
from datetime import datetime, timezone

from twin.config import PIPELINE, SYNC, HampelSpec
from twin.ingest import Deduplicator, LossDetector, Measurement, Rejected, ReorderBuffer, parse_message
from twin.preprocessing import HampelFilter, StreamResampler
from twin.processor import StreamProcessor
from twin.sync import SyncPolicy


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def payload(value, ts, seq=1, unit="Celsius", sensor="nozzle_temp_01") -> bytes:
    return json.dumps({"sensor_id": sensor, "seq_id": seq, "value": value, "unit": unit, "timestamp": iso(ts)}).encode()


def meas(ts, value=1.0, seq=1, metric="nozzle_temperature", t_recv=None) -> Measurement:
    t_recv = ts if t_recv is None else t_recv
    return Measurement("p1", metric, "s", seq, value, "Celsius", ts, t_recv, ts)


T0 = 1_800_000_000.0
NOZZLE = "factory/p1/telemetry/nozzle_temperature"


class ValidationTest(unittest.TestCase):
    def test_valid_message(self):
        m = parse_message(NOZZLE, payload(210.5, T0), T0 + 0.01, T0 + 0.01, PIPELINE)
        self.assertIsInstance(m, Measurement)
        self.assertAlmostEqual(m.ts, T0, places=5)
        self.assertEqual((m.printer_id, m.metric, m.value), ("p1", "nozzle_temperature", 210.5))

    def test_rejections(self):
        cases = {
            b"{not json": "malformed_json",
            json.dumps({"value": 1}).encode(): "missing_field",
            payload("NaN", T0): "wrong_type",
            payload(-999.0, T0): "out_of_range",
            payload(210.0, T0, unit="F"): "unit_mismatch",
            payload(210.0, T0 + 60): "timestamp_in_future",
        }
        for raw, reason in cases.items():
            r = parse_message(NOZZLE, raw, T0, T0, PIPELINE)
            self.assertIsInstance(r, Rejected, raw)
            self.assertEqual(r.reason, reason, raw)
        self.assertEqual(parse_message("factory/p1/telemetry/unknown", payload(1, T0), T0, T0, PIPELINE).reason, "unknown_metric")

    def test_status_message(self):
        s = parse_message("factory/p1/status", b'{"status": "OFFLINE", "reason": "unexpected_disconnect"}', T0, T0, PIPELINE)
        self.assertEqual((s.printer_id, s.status), ("p1", "OFFLINE"))


class BufferingTest(unittest.TestCase):
    def test_reorder_buffer_orders_by_event_time(self):
        rb = ReorderBuffer(0.2)
        for ts in (T0 + 0.10, T0 + 0.00, T0 + 0.05):          # arrive out of order
            self.assertIsNone(rb.push(meas(ts)))
        self.assertEqual(rb.pop_ready(T0 + 0.15), [])            # nothing older than the watermark yet
        out = rb.pop_ready(T0 + 0.31)
        self.assertEqual([m.ts for m in out], [T0, T0 + 0.05, T0 + 0.10])

    def test_late_sample_bypasses_buffer(self):
        rb = ReorderBuffer(0.1)
        rb.push(meas(T0 + 1.0))
        rb.pop_ready(T0 + 2.0)
        late = rb.push(meas(T0 + 0.5))
        self.assertTrue(late is not None and late.late)

    def test_dedup_and_loss(self):
        d = Deduplicator(10)
        self.assertFalse(d.is_duplicate(meas(T0, seq=1)))
        self.assertTrue(d.is_duplicate(meas(T0, seq=1)))
        self.assertFalse(d.is_duplicate(meas(T0 + 1, seq=1)))  # publisher restart: same seq, new time
        ld = LossDetector()
        self.assertEqual([ld.observe(meas(T0, seq=s)) for s in (1, 2, 5, 6)], [0, 0, 2, 0])


class HampelTest(unittest.TestCase):
    def test_spike_is_replaced_by_median(self):
        f = HampelFilter(HampelSpec(window=9, k=3.5, min_sigma=0.4))
        for v in [210.0, 210.2, 209.9, 210.1, 210.0, 209.8, 210.3, 210.1, 210.0]:
            f.update(v)
        value, outlier, median = f.update(245.0)
        self.assertTrue(outlier)
        self.assertEqual(value, 210.0)            # last accepted value is kept
        self.assertLess(abs(median - 210.0), 0.5)
        self.assertFalse(f.update(210.2)[1])

    def test_trend_then_spike(self):
        """Regression (found in the outage run): slow sine trend of the emulator, then a +35 C spike.
        The trend must not be flagged and the spike must not slip through after a level shift."""
        spec = HampelSpec(window=7, k=3.5, min_sigma=0.7)
        f = HampelFilter(spec)
        trend = [210 + 2 * math.sin((i * 10) / 50.0) for i in range(60)]   # 1 Hz samples, max 0.4 C/s
        flags = [f.update(v)[1] for v in trend]
        self.assertEqual(sum(flags), 0)
        f = HampelFilter(HampelSpec(window=9, k=3.5, min_sigma=0.4))
        for v in [208.4, 208.4, 208.4, 208.4, 208.4, 208.4, 208.4, 208.4, 208.4, 210.4, 210.9, 211.1]:
            f.update(v)                                                    # forces a level shift
        value, outlier, _ = f.update(245.9)
        self.assertTrue(outlier)
        self.assertLess(value, 212.0)

    def test_level_shift_is_accepted_after_persistence(self):
        f = HampelFilter(HampelSpec(window=9, k=3.5, min_sigma=0.4, persistence=3))
        for _ in range(9):
            f.update(60.0)
        flags = [f.update(80.0)[1] for _ in range(4)]
        self.assertEqual(flags, [True, True, False, False])

    def test_spike_during_level_shift_does_not_pass(self):
        """Regression (found in lab 4, thermal runaway): ramp +2 C/s, the third confirming sample is a +35 C
        spike; the accepted level must be the ramp, not the spike."""
        f = HampelFilter(HampelSpec(window=7, k=3.5, min_sigma=0.7))
        for v in [210.0, 210.3, 209.8, 210.1, 210.2, 209.9, 210.0]:
            f.update(v)
        out = [f.update(v) for v in [214.0, 216.0, 218.0 + 35.0]]
        self.assertEqual([o[1] for o in out], [True, True, False])
        self.assertAlmostEqual(out[2][0], 216.0)


class IndicatorTest(unittest.TestCase):
    def test_thermal_state_hysteresis(self):
        from twin.model import DeviceModel
        m = DeviceModel("p1")
        states = []
        for i, t in enumerate([210, 216, 226, 224.5, 225.5, 223.5, 214.5, 213.9, 210]):
            m.apply_measurement("nozzle_temperature", T0 + i, t, False)
            states.append(m.values["thermal_state"])
        # no CRITICAL <-> WARNING chattering around 225 C, falls back only below 224 / 214 C
        self.assertEqual(states, ["NORMAL", "WARNING", "CRITICAL", "CRITICAL", "CRITICAL", "WARNING",
                                  "WARNING", "NORMAL", "NORMAL"])


class ResamplerTest(unittest.TestCase):
    def setUp(self):
        self.r = StreamResampler("p1", "m", PIPELINE)   # 1 s grid, LOCF <= 5 s, interpolation <= 10 s

    def test_downsampling_mean_min_max(self):
        for i in range(10):
            self.r.add(T0 + i * 0.1, float(i))
        (p,) = self.r.close_until(math.floor(T0) + 1)
        self.assertEqual((p.fill, p.count, p.vmin, p.vmax), ("measured", 10, 0.0, 9.0))
        self.assertAlmostEqual(p.mean, 4.5)

    def test_gap_locf_then_interpolation(self):
        k0 = math.floor(T0)
        self.r.add(k0 + 0.5, 10.0)
        out = self.r.close_until(k0 + 4)                       # buckets k0..k0+3, three are empty
        self.assertEqual([p.fill for p in out], ["measured", "locf", "locf", "locf"])
        self.r.add(k0 + 4.5, 14.0)
        out = self.r.close_until(k0 + 5)
        fills = {p.bucket - k0: (p.fill, round(p.mean, 3), p.correction) for p in out}
        self.assertEqual(fills[4], ("measured", 14.0, False))
        self.assertEqual(fills[1], ("interpolated", 11.0, True))
        self.assertEqual(fills[3], ("interpolated", 13.0, True))

    def test_long_gap_becomes_missing_and_late_data_backfills(self):
        k0 = math.floor(T0)
        self.r.add(k0 + 0.5, 1.0)
        out = self.r.close_until(k0 + 8)
        self.assertEqual([p.fill for p in out], ["measured"] + ["locf"] * 5 + ["missing"] * 2)
        corr = self.r.add(k0 + 6.5, 7.0)                       # store-and-forward sample arrives later
        by_bucket = {p.bucket - k0: p.fill for p in corr}
        self.assertEqual(by_bucket[6], "measured")
        self.assertEqual(by_bucket[3], "interpolated")         # LOCF replaced by interpolation 1 -> 7


class SyncPolicyTest(unittest.TestCase):
    def test_deadband(self):
        p = SyncPolicy(SYNC)
        committed = {"nozzle_temperature": 210.0, "stepper_current_x": 1.20}
        self.assertEqual(p.threshold_exceeded({"nozzle_temperature": 210.6}, committed, ["nozzle_temperature"]), [])
        self.assertEqual(p.threshold_exceeded({"nozzle_temperature": 210.75}, committed, ["nozzle_temperature"]),
                         ["nozzle_temperature"])
        self.assertEqual(p.threshold_exceeded({"stepper_current_x": 1.26}, committed, ["stepper_current_x"]),
                         ["stepper_current_x"])


class ProcessorTest(unittest.TestCase):
    """End-to-end through the processor with a fake clock (no broker, no database)."""

    def setUp(self):
        self.now = T0
        self.lines = []
        self.proc = StreamProcessor(sink=self.lines.append, clock=lambda: self.now, wall_offset=0.0)
        self.notes = []
        self.proc.store.subscribe(self.notes.append)

    def feed(self, metric, value, ts, seq, unit):
        """ts - device timestamp; the message arrives 5 ms later by the server monotonic clock."""
        self.proc.submit(f"factory/p1/telemetry/{metric}", payload(value, ts, seq, unit), self.now + 0.005)

    def advance(self, dt):
        self.now += dt
        self.proc.run_once(block_s=0)

    def test_threshold_event_and_periodic_rules(self):
        seq = 0
        for i in range(60):                                       # 6 s of feed rate at 10 Hz
            seq += 1
            value = 50.0 if i < 30 else 120.0                     # PRINTING -> HIGH_SPEED at 3 s
            self.feed("extruder_feed_rate", value, self.now, seq, "mm/s")
            self.advance(0.1)
        self.advance(0.5)
        reasons = [n.reason for n in self.notes]
        self.assertIn("PERIODIC", reasons)
        self.assertIn("THRESHOLD", reasons)
        modes = [e["new"] for n in self.notes for e in n.events if e["type"] == "MODE_CHANGE"]
        self.assertEqual(modes, ["PRINTING", "HIGH_SPEED"])
        state = self.proc.store.devices["p1"].committed
        self.assertEqual(state["operating_mode"], "HIGH_SPEED")
        self.assertEqual(state["extruder_feed_rate"], 120.0)
        self.assertGreater(state["extruded_length_mm"], 0)
        self.assertTrue(any(ln.startswith("telemetry_raw,") for ln in self.lines))
        self.assertTrue(any(ln.startswith("telemetry_resampled,") for ln in self.lines))
        self.assertTrue(any(ln.startswith("twin_state,") for ln in self.lines))

    def test_watchdog_marks_stale_and_recovers(self):
        self.feed("nozzle_temperature", 210.0, self.now, 1, "Celsius")
        self.advance(0.3)
        for _ in range(40):
            self.advance(0.1)
        conn = [e["new"] for n in self.notes for e in n.events if e["type"] == "CONNECTIVITY"]
        self.assertEqual(conn, ["ONLINE", "STALE"])
        self.feed("nozzle_temperature", 210.1, self.now, 2, "Celsius")
        self.advance(0.3)
        self.assertEqual(self.proc.store.devices["p1"].committed["connectivity"], "ONLINE")

    def test_event_time_is_independent_from_server_clock(self):
        """Device clock 10 s behind the server, temperatures delivered 2 s later than the feed rate:
        per-stream watermarks still release everything in order, nothing is late or corrected."""
        skew, seq = -10.0, 0
        for i in range(50):
            gen = self.now + skew
            seq += 1
            self.feed("extruder_feed_rate", 50.0, gen, seq, "mm/s")
            if i % 10 == 0 and i >= 20:   # QoS 1 stream: generated 2 s earlier, arrives now
                self.feed("nozzle_temperature", 210.0, gen - 2.0, i, "Celsius")
            self.advance(0.1)
        self.advance(1.0)
        c = self.proc.metrics.counters
        self.assertEqual((c["late"], c["resample_corrections"]), (0, 0))
        self.assertGreater(c["resampled_points"], 4)
        self.assertEqual(self.proc.store.devices["p1"].committed["nozzle_temperature"], 210.0)

    def test_duplicates_and_invalid_are_counted(self):
        self.feed("nozzle_temperature", 210.0, self.now, 1, "Celsius")
        self.feed("nozzle_temperature", 210.0, self.now, 1, "Celsius")
        self.proc.submit("factory/p1/telemetry/nozzle_temperature", b"{broken", self.now)
        self.advance(0.3)
        c = self.proc.metrics.counters
        self.assertEqual((c["duplicate"], c["invalid"], c["processed"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
