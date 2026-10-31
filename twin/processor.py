"""Stream processor: wires ingestion -> preprocessing -> state update -> sync rules -> storage.

All stages run in one processing thread (deterministic, no locks on the twin state). The MQTT network
thread only timestamps the message and puts it into the inbox queue (buffering).

Two time scales are used:
  event time      - device timestamps; drives ordering, resampling buckets and derived windows
                    (per-stream watermark, see ingest.EventClock);
  processing time - the local monotonic clock; drives timers (watchdog, periodic sync, metrics)
                    and latency measurement. Wall-clock time is only used to timestamp stored records
                    (wall = start offset + monotonic, so stored commit times never jump backwards).
"""
import logging
import queue
import time

from .config import METRICS, PIPELINE, SYNC, PipelineConfig, SyncConfig
from .ingest import Deduplicator, LossDetector, Measurement, Rejected, ReorderBuffer, StatusMessage, parse_message
from .metrics import PipelineMetrics
from .preprocessing import HampelFilter, ResampledPoint, Resampler
from .state_store import Notification, TwinStore
from .storage import line
from .sync import SyncPolicy, SyncReason

log = logging.getLogger("twin.processor")

TICK_S = 0.05
METRICS_INTERVAL_S = 1.0


class StreamProcessor:
    def __init__(self, pcfg: PipelineConfig = PIPELINE, scfg: SyncConfig = SYNC, sink=None,
                 metrics: PipelineMetrics | None = None, run_id: str = "live",
                 clock=time.monotonic, wall_offset: float | None = None):
        self.pcfg, self.scfg, self.run_id = pcfg, scfg, run_id
        self.mono = clock
        self.wall_offset = time.time() - clock() if wall_offset is None else wall_offset
        self.sink = sink or (lambda ln: None)
        self.metrics = metrics or PipelineMetrics()
        self.inbox: queue.Queue = queue.Queue()
        self.reorder = ReorderBuffer(pcfg.reorder_delay_s)
        self.dedup = Deduplicator(pcfg.dedup_window)
        self.loss = LossDetector()
        self.filters: dict[tuple, HampelFilter | None] = {}
        self.resampler = Resampler(pcfg)
        self.policy = SyncPolicy(scfg)
        self.store = TwinStore()
        self.store.subscribe(self._on_commit)
        self.metric_rows: list = []          # callbacks for per-second metric rows
        self.gauges = lambda: {}             # extra gauges supplied by the host (writer backlog, ...)
        self._next_tick = 0.0
        self._next_metrics = clock() + METRICS_INTERVAL_S
        self._rejected_logged = 0

    def wall(self, mono: float | None = None) -> float:
        return self.wall_offset + (self.mono() if mono is None else mono)

    # --- entry point from the MQTT thread ------------------------------------------------------------
    def submit(self, topic: str, payload: bytes, t_recv: float | None = None):
        self.metrics.on_received()
        self.inbox.put((topic, payload, self.mono() if t_recv is None else t_recv))

    # --- main loop -----------------------------------------------------------------------------------
    def run_once(self, block_s: float = 0.01):
        now = self.mono()
        timeout = block_s
        nr = self.reorder.next_release()
        if nr is not None:
            timeout = min(timeout, max(0.0, nr - now))
        try:
            item = self.inbox.get(timeout=timeout)
            self.handle_raw(*item)
            for _ in range(5000):
                self.handle_raw(*self.inbox.get_nowait())
        except queue.Empty:
            pass
        now = self.mono()
        for m in self.reorder.pop_ready(now):
            if m.late:
                self.handle_late(m)
            else:
                self.process(m)
        if now >= self._next_tick:
            self.tick(now)
            self._next_tick = now + TICK_S

    def drain(self):
        """Processes everything buffered (used at shutdown and in tests)."""
        while True:
            try:
                self.handle_raw(*self.inbox.get_nowait())
            except queue.Empty:
                break
        for m in self.reorder.flush():
            self.handle_late(m) if m.late else self.process(m)
        self.tick(self.mono())

    # --- stage 1: validation, de-duplication, buffering --------------------------------------------
    def handle_raw(self, topic: str, payload: bytes, t_recv: float):
        msg = parse_message(topic, payload, self.wall(t_recv), t_recv, self.pcfg)
        if isinstance(msg, Measurement):
            if self.dedup.is_duplicate(msg):
                self.metrics.count("duplicate")
                return
            dev = self.store.device(msg.printer_id, t_recv)
            dev.last_seen = t_recv
            dev.model.record_arrival(t_recv)
            if dev.model.values["connectivity"] != "ONLINE":
                # connectivity is observed now (processing time): the first message after a reconnect
                # may be an old one from the device's store-and-forward buffer
                events = dev.model.set_connectivity("ONLINE", self.wall(t_recv), "telemetry received")
                self._commit_events(dev, events, msg)
            late = self.reorder.push(msg)
            if late is not None:
                self.handle_late(late)
        elif isinstance(msg, StatusMessage):
            self.metrics.count("status")
            dev = self.store.device(msg.printer_id, t_recv)
            status = "ONLINE" if msg.status.upper() == "ONLINE" else "OFFLINE"
            events = dev.model.set_connectivity(status, self.wall(t_recv),
                                                f"status topic: {msg.status} {msg.reason}".strip())
            self._commit_events(dev, events, None)
        else:
            self._reject(msg, t_recv)

    def _reject(self, r: Rejected, t_recv: float):
        self.metrics.count("invalid")
        if r.printer_id and r.printer_id in self.store.devices:
            self.store.devices[r.printer_id].model.record_quality(t_recv, bad=1)
        self.sink(line("telemetry_rejected", {"printer_id": r.printer_id, "metric": r.metric, "reason": r.reason},
                       {"payload": r.payload[:500]}, self.wall(t_recv)))
        if self._rejected_logged < 20:
            self._rejected_logged += 1
            log.warning("[VALIDATION] %s rejected (%s): %.120s", r.topic, r.reason, r.payload)

    # --- stage 2: preprocessing + state update + sync rules ----------------------------------------
    def process(self, m: Measurement):
        now = self.mono()
        dev = self.store.device(m.printer_id, now)
        model = dev.model
        lost = self.loss.observe(m)
        if lost:
            self.metrics.count("lost", lost)
            model.record_quality(now, bad=lost)

        flt = self.filters.get(m.stream, False)
        if flt is False:
            spec = METRICS[m.metric].hampel
            flt = self.filters[m.stream] = HampelFilter(spec) if spec else None
        filtered, outlier, median = flt.update(m.value) if flt else (m.value, False, m.value)

        events = []
        if outlier:
            self.metrics.count("outlier")
            events.append(model.record_outlier(m.metric, m.ts, m.value, median, filtered, now))
        else:
            model.record_quality(now, good=1)
        events += model.apply_measurement(m.metric, m.ts, filtered, outlier)
        self.metrics.on_processed(m.t_gen, m.t_recv, self.mono())

        self.sink(line("telemetry_raw",
                       {"printer_id": m.printer_id, "metric": m.metric, "sensor_id": m.sensor_id, "unit": m.unit},
                       {"value": m.value, "filtered": filtered, "seq_id": m.seq_id, "outlier": outlier,
                        "late": False, "net_latency_ms": round((m.t_recv - m.t_gen) * 1e3, 3)}, m.ts))
        self._write_resampled(self.resampler.add(m.printer_id, m.metric, m.ts, filtered))

        # sync rules: EVENT first (it already carries the measured value), then THRESHOLD
        if events:
            self._commit_events(dev, events, m, extra=[m.metric])
        exceeded = self.policy.threshold_exceeded(model.values, dev.committed, [m.metric])
        if exceeded:
            t = self.mono()
            self.store.commit(dev, SyncReason.THRESHOLD, exceeded, self.wall(t), m.ts, (t - m.t_gen) * 1e3)

    def handle_late(self, m: Measurement):
        """A sample older than the current state: kept in history, corrects the resampled series,
        but never moves the twin state backwards in time."""
        self.metrics.count("late")
        self.sink(line("telemetry_raw",
                       {"printer_id": m.printer_id, "metric": m.metric, "sensor_id": m.sensor_id, "unit": m.unit},
                       {"value": m.value, "seq_id": m.seq_id, "outlier": False, "late": True,
                        "net_latency_ms": round((m.t_recv - m.t_gen) * 1e3, 3)}, m.ts))
        self._write_resampled(self.resampler.add(m.printer_id, m.metric, m.ts, m.value))

    def _commit_events(self, dev, events, trigger: Measurement | None, extra=()):
        if not events:
            return
        attrs = list(extra)
        for e in events:
            attrs += [a for a in self.policy.event_attributes(e.attribute) if a not in attrs]
        self.metrics.count("events", len(events))
        t = self.mono()
        latency = (t - trigger.t_gen) * 1e3 if trigger else None
        self.store.commit(dev, SyncReason.EVENT, attrs, self.wall(t), trigger.ts if trigger else None, latency, events)

    def _write_resampled(self, points: list[ResampledPoint]):
        p = self.pcfg.resample_period_s
        for pt in points:
            self.metrics.count("resample_corrections" if pt.correction else "resampled_points")
            self.sink(line("telemetry_resampled", {"printer_id": pt.printer_id, "metric": pt.metric},
                           {"mean": pt.mean, "min": pt.vmin, "max": pt.vmax, "count": pt.count, "fill": pt.fill},
                           pt.bucket * p))

    # --- stage 3: timers (resampling grid, watchdog, periodic rule, metrics) ------------------------
    def tick(self, now: float):
        watermarks = {s: self.reorder.watermark(s, now) for s in self.reorder.clock.streams()}
        self._write_resampled(self.resampler.close(watermarks))
        wall = self.wall(now)
        for dev in list(self.store.devices.values()):
            model = dev.model
            model.refresh(now)
            conn = model.values["connectivity"]
            if conn == "ONLINE" and dev.last_seen and self.policy.is_stale(dev.last_seen, now):
                events = model.set_connectivity("STALE", wall, f"no telemetry for {now - dev.last_seen:.1f} s")
                self._commit_events(dev, events, None)
            exceeded = self.policy.threshold_exceeded(model.values, dev.committed, ["health_index"])
            if exceeded:
                self.store.commit(dev, SyncReason.THRESHOLD, exceeded, wall)
            dormant = conn in ("OFFLINE", "STALE") and now - dev.last_seen > 30.0
            if not dormant and self.policy.periodic_due(dev.last_periodic, now):
                dev.last_periodic = now
                self.store.commit(dev, SyncReason.PERIODIC, None, wall)
        if now >= self._next_metrics:
            interval = METRICS_INTERVAL_S + (now - self._next_metrics)
            self._next_metrics = now + METRICS_INTERVAL_S
            row = self.metrics.roll(wall, interval, {"inbox_depth": self.inbox.qsize(),
                                                     "reorder_depth": len(self.reorder), **self.gauges()})
            row["run_id"] = self.run_id
            self.sink(line("pipeline_metrics", {"run_id": self.run_id},
                           {k: v for k, v in row.items() if k not in ("ts", "run_id")}, wall))
            for cb in self.metric_rows:
                cb(row)

    # --- commit subscriber: history of states and events ----------------------------------------------
    def _on_commit(self, n: Notification):
        self.metrics.on_commit(n.reason, n.latency_ms if n.reason != SyncReason.PERIODIC else None)
        dev = self.store.devices[n.printer_id]
        fields = {k: v for k, v in dev.committed.items() if k != "printer_id"}
        self.sink(line("twin_state", {"printer_id": n.printer_id, "reason": n.reason}, fields, n.commit_ts))
        for e in n.events:
            self.sink(line("twin_events", {"printer_id": n.printer_id, "type": e["type"], "severity": e["severity"]},
                           {"attribute": e["attribute"], "old": str(e["old"]), "new": str(e["new"]),
                            "message": e["message"]}, e["ts"]))
