"""Ingestion stage: parsing + validation of MQTT messages, de-duplication, reorder buffer, loss detection."""
import heapq
import itertools
import json
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .config import METRICS, PipelineConfig

REQUIRED_FIELDS = ("sensor_id", "seq_id", "value", "unit", "timestamp")


@dataclass
class Measurement:
    printer_id: str
    metric: str
    sensor_id: str
    seq_id: int
    value: float
    unit: str
    ts: float            # generation time (event time) by the device clock, epoch seconds
    t_recv: float        # arrival at the pipeline, monotonic clock
    t_gen: float         # generation time on the pipeline's monotonic clock (for latency measurement)
    late: bool = False   # arrived after a newer sample of the same stream had been released

    @property
    def stream(self) -> tuple[str, str]:
        return self.printer_id, self.metric


@dataclass
class StatusMessage:
    printer_id: str
    status: str
    reason: str
    ts: float
    t_recv: float


@dataclass
class Rejected:
    reason: str
    topic: str
    payload: str
    printer_id: str = ""
    metric: str = ""


def parse_timestamp(s: str) -> float:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return dt.timestamp()


def parse_message(topic: str, payload: bytes, t_recv_wall: float, t_recv: float, cfg: PipelineConfig):
    """Validates one MQTT message. Returns Measurement | StatusMessage | Rejected.

    t_recv_wall - arrival time by the pipeline wall clock (compared with device timestamps),
    t_recv      - arrival time by the monotonic clock.
    If the payload carries the optional diagnostic field t_mono (monotonic generation time, emulator on
    the same host), latency is measured on the monotonic clock; otherwise wall clocks are compared.
    """
    parts = topic.split("/")
    text = payload.decode("utf-8", errors="replace")
    if len(parts) == 3 and parts[0] == "factory" and parts[2] == "status":
        try:
            data = json.loads(text)
            ts = parse_timestamp(data["timestamp"]) if "timestamp" in data else t_recv_wall
            return StatusMessage(parts[1], str(data["status"]), str(data.get("reason", "")), ts, t_recv)
        except (ValueError, KeyError, TypeError):
            return Rejected("bad_status", topic, text, parts[1])
    if len(parts) != 4 or parts[0] != "factory" or parts[2] != "telemetry":
        return Rejected("unknown_topic", topic, text)
    printer_id, metric = parts[1], parts[3]
    spec = METRICS.get(metric)
    if spec is None:
        return Rejected("unknown_metric", topic, text, printer_id, metric)

    def reject(reason):
        return Rejected(reason, topic, text, printer_id, metric)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return reject("malformed_json")
    if not isinstance(data, dict) or not all(f in data for f in REQUIRED_FIELDS):
        return reject("missing_field")
    value, seq_id = data["value"], data["seq_id"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or isinstance(seq_id, bool) or not isinstance(seq_id, int):
        return reject("wrong_type")
    if not math.isfinite(value):
        return reject("non_finite")
    if data["unit"] != spec.unit:
        return reject("unit_mismatch")
    if not spec.valid_min <= value <= spec.valid_max:
        return reject("out_of_range")
    try:
        ts = parse_timestamp(str(data["timestamp"]))
    except ValueError:
        return reject("bad_timestamp")
    if ts > t_recv_wall + cfg.max_future_skew_s:
        return reject("timestamp_in_future")
    if ts < t_recv_wall - cfg.max_age_s:
        return reject("timestamp_too_old")
    t_mono = data.get("t_mono")
    same_host = isinstance(t_mono, float) and 0.0 <= t_recv - t_mono < cfg.max_age_s
    t_gen = t_mono if same_host else t_recv - (t_recv_wall - ts)
    return Measurement(printer_id, metric, str(data["sensor_id"]), seq_id, float(value),
                       spec.unit, ts, t_recv, t_gen)


class Deduplicator:
    """At-least-once delivery (QoS 1) may repeat messages: drop exact repeats of (seq_id, timestamp)."""

    def __init__(self, window: int):
        self.window = window
        self._seen: dict[tuple, tuple[deque, set]] = {}

    def is_duplicate(self, m: Measurement) -> bool:
        order, keys = self._seen.setdefault(m.stream, (deque(), set()))
        key = (m.seq_id, m.ts)
        if key in keys:
            return True
        keys.add(key)
        order.append(key)
        if len(order) > self.window:
            keys.discard(order.popleft())
        return False


class LossDetector:
    """Detects lost messages from gaps in seq_id of the *ordered* stream (idea from lab 2)."""

    def __init__(self):
        self._last: dict[tuple, int] = {}

    def observe(self, m: Measurement) -> int:
        last = self._last.get(m.stream)
        self._last[m.stream] = m.seq_id if last is None else max(last, m.seq_id)
        if last is None or m.seq_id <= last:
            return 0   # first sample, publisher restart or already counted
        return m.seq_id - last - 1


@dataclass(order=True)
class _Held:
    ts: float
    order: int
    item: Measurement = field(compare=False)


class EventClock:
    """Per-stream event-time clock: the largest device timestamp seen in a (printer, metric) stream,
    extrapolated with the local monotonic clock while the stream is silent (idleness). It does not depend
    on the server wall clock, so device/server clock offsets and wall-clock steps do not break event-time
    processing; per-stream clocks also tolerate streams of one device delivered with different delays
    (QoS 0 vs QoS 1 under load)."""

    def __init__(self):
        self._max: dict[tuple, tuple[float, float]] = {}   # stream -> (max event ts, monotonic time seen)

    def observe(self, stream: tuple, ts: float, mono: float):
        cur = self._max.get(stream)
        if cur is None or ts > cur[0]:
            self._max[stream] = (ts, mono)

    def now(self, stream: tuple, mono: float) -> float | None:
        cur = self._max.get(stream)
        return None if cur is None else cur[0] + (mono - cur[1])

    def streams(self):
        return self._max.keys()


class ReorderBuffer:
    """Bounded out-of-orderness buffer with a per-stream watermark W = stream event clock - delay.

    Samples are released in event-time order once ts <= W, so samples delayed by the network less than
    `delay` relative to their neighbours come out ordered. A sample older than the last released sample
    of its stream is marked late and bypasses the buffer.
    """

    def __init__(self, delay_s: float, clock: EventClock | None = None):
        self.delay = delay_s
        self.clock = clock or EventClock()
        self._heaps: dict[tuple, list[_Held]] = {}
        self._counter = itertools.count()
        self._last_released: dict[tuple, float] = {}

    def __len__(self):
        return sum(len(h) for h in self._heaps.values())

    def watermark(self, stream: tuple, mono: float) -> float | None:
        t = self.clock.now(stream, mono)
        return None if t is None else t - self.delay

    def push(self, m: Measurement) -> Measurement | None:
        """Returns the sample back if it is late (bypasses the buffer), otherwise buffers it."""
        self.clock.observe(m.stream, m.ts, m.t_recv)
        last = self._last_released.get(m.stream)
        if last is not None and m.ts < last:
            m.late = True
            return m
        heapq.heappush(self._heaps.setdefault(m.stream, []), _Held(m.ts, next(self._counter), m))
        return None

    def pop_ready(self, mono: float) -> list[Measurement]:
        out = []
        for stream, heap in self._heaps.items():
            wm = self.watermark(stream, mono)
            while heap and heap[0].ts <= wm:
                m = heapq.heappop(heap).item
                self._last_released[stream] = m.ts
                out.append(m)
        out.sort(key=lambda m: m.ts)
        return out

    def next_release(self) -> float | None:
        """Monotonic time at which the earliest buffered sample becomes ready."""
        times = []
        for stream, heap in self._heaps.items():
            if heap:
                max_ts, seen = self.clock._max[stream]
                times.append(heap[0].ts - max_ts + seen + self.delay)
        return min(times) if times else None

    def flush(self) -> list[Measurement]:
        return self.pop_ready(float("inf"))
