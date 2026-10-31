"""In-memory twin state store with versioned commits and subscriber notifications (observer pattern)."""
import logging
import queue
import threading
from dataclasses import dataclass, field

from .config import METRICS
from .model import ATTRIBUTES, DeviceModel, ModelEvent

log = logging.getLogger("twin.store")


@dataclass
class Notification:
    printer_id: str
    version: int
    reason: str                    # PERIODIC | EVENT | THRESHOLD
    changes: dict                  # attribute -> new value (only attributes that changed)
    snapshot: dict                 # full committed state (JSON-friendly)
    events: list[dict]
    commit_ts: float               # wall clock of the commit
    trigger_ts: float | None       # event time of the measurement that triggered the commit
    latency_ms: float | None       # generation of the trigger measurement -> commit (monotonic clock)


@dataclass
class DeviceTwin:
    model: DeviceModel
    committed: dict = field(default_factory=dict)
    committed_meta: dict = field(default_factory=dict)
    version: int = 0
    last_seen: float = 0.0          # monotonic time of the last telemetry message
    last_periodic: float = 0.0      # monotonic time of the last periodic commit
    snapshot: dict | None = None    # last published snapshot (read by other threads)


class TwinStore:
    def __init__(self):
        self.devices: dict[str, DeviceTwin] = {}
        self._subscribers: list = []
        self._lock = threading.Lock()
        self.commit_counts = {"PERIODIC": 0, "EVENT": 0, "THRESHOLD": 0}

    # --- device registry -----------------------------------------------------------------------
    def device(self, printer_id: str, mono_now: float = 0.0) -> DeviceTwin:
        d = self.devices.get(printer_id)
        if d is None:
            d = DeviceTwin(DeviceModel(printer_id), last_periodic=mono_now)
            with self._lock:
                self.devices[printer_id] = d
            log.info("[STORE] New digital twin instance: %s", printer_id)
        return d

    def snapshots(self) -> dict[str, dict]:
        with self._lock:
            return {pid: d.snapshot for pid, d in self.devices.items() if d.snapshot}

    # --- subscriptions -------------------------------------------------------------------------
    def subscribe(self, callback):
        """callback(Notification) is invoked synchronously in the processing thread: keep it fast."""
        with self._lock:
            self._subscribers.append(callback)
        return lambda: self.unsubscribe(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    # --- commit --------------------------------------------------------------------------------
    def commit(self, d: DeviceTwin, reason: str, attrs: list[str] | None, now: float,
               trigger_ts: float | None = None, latency_ms: float | None = None,
               events: list[ModelEvent] = ()) -> Notification | None:
        """now - wall-clock commit time (stored in the history), attrs=None commits everything."""
        working = d.model.values
        attrs = list(working) if attrs is None else attrs
        changes = {}
        for a in attrs:
            new = working.get(a)
            if d.committed.get(a, object()) != new:
                changes[a] = new
            d.committed[a] = new
            if a in METRICS:
                d.committed_meta[a] = dict(d.model.meta[a])
        if not changes and not events and reason != "PERIODIC":
            return None
        d.version += 1
        d.committed["version"] = d.version
        self.commit_counts[reason] += 1
        snapshot = self._snapshot(d, reason, now)
        d.snapshot = snapshot
        n = Notification(d.model.values["printer_id"], d.version, reason, changes, snapshot,
                         [_event_dict(d.model.values["printer_id"], e) for e in events], now, trigger_ts, latency_ms)
        for cb in list(self._subscribers):
            try:
                cb(n)
            except Exception:  # a faulty subscriber must not break the pipeline
                log.exception("[STORE] subscriber failed")
        return n

    def _snapshot(self, d: DeviceTwin, reason: str, now: float) -> dict:
        c = d.committed
        groups = {"measurements": {}, "derived": {}, "indicators": {}, "counters": {}}
        for spec in ATTRIBUTES:
            if spec.group == "measured":
                meta = d.committed_meta.get(spec.name, {})
                groups["measurements"][spec.name] = {
                    "value": c.get(spec.name), "unit": spec.unit,
                    "ts": meta.get("ts"), "quality": meta.get("quality")}
            elif spec.group in ("derived", "indicator", "counter"):
                key = {"derived": "derived", "indicator": "indicators", "counter": "counters"}[spec.group]
                groups[key][spec.name] = c.get(spec.name)
        return {"printer_id": d.model.values["printer_id"], "version": d.version, "reason": reason,
                "committed_at": now, "connectivity": c.get("connectivity"),
                "last_event_ts": c.get("last_event_ts"), **groups}


def _event_dict(printer_id: str, e: ModelEvent) -> dict:
    return {"printer_id": printer_id, "type": e.type, "severity": e.severity, "attribute": e.attribute,
            "old": e.old, "new": e.new, "ts": e.ts, "message": e.message}


class QueueSubscriber:
    """Decouples a slow consumer (web client, MQTT republisher) from the processing thread.
    The queue is bounded; on overflow the oldest notification is dropped (latest state wins)."""

    def __init__(self, maxsize: int = 1000):
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def __call__(self, n: Notification):
        while True:
            try:
                self.q.put_nowait(n)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass
