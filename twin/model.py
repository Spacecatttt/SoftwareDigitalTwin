"""Digital twin state model of a 3D printer.

The state is a flat dict of attributes (easy to diff, to serialise and to store as InfluxDB fields).
Attributes are grouped into: meta, measured (filtered sensor values), derived (aggregates over
event-time windows) and indicators (discrete operating modes / alarm levels).
"""
import math
from collections import deque
from dataclasses import dataclass

from .config import METRICS, MODEL, ModelConfig


@dataclass(frozen=True)
class AttributeSpec:
    name: str
    type: str
    unit: str
    group: str      # meta | measured | derived | indicator | counter
    rule: str       # how the value is obtained (used in the report and in the web UI)


ATTRIBUTES: list[AttributeSpec] = [
    AttributeSpec("printer_id", "str", "", "meta", "MQTT topic level factory/{printer_id}/..."),
    AttributeSpec("connectivity", "enum", "", "meta",
                  "ONLINE/OFFLINE from status topic (birth/LWT); STALE if no telemetry > stale_timeout"),
    AttributeSpec("last_event_ts", "timestamp", "s", "meta", "event time of the latest applied measurement"),
    *[AttributeSpec(m.name, "float", m.unit, "measured",
                    f"{m.nominal_hz:g} Hz sensor value after validation"
                    + (", Hampel outlier filter" if m.hampel else "")) for m in METRICS.values()],
    AttributeSpec("nozzle_temp_avg_30s", "float", "Celsius", "derived", "moving average of nozzle temperature, 30 s window"),
    AttributeSpec("nozzle_temp_rate", "float", "Celsius/s", "derived", "least-squares slope of nozzle temperature, 10 s window"),
    AttributeSpec("feed_rate_avg_5s", "float", "mm/s", "derived", "moving average of extruder feed rate, 5 s window"),
    AttributeSpec("stepper_current_rms_5s", "float", "A", "derived", "RMS of X stepper current, 5 s window"),
    AttributeSpec("extruded_length_mm", "float", "mm", "derived", "trapezoidal integral of feed rate over time"),
    AttributeSpec("filament_used_g", "float", "g", "derived", "extruded length x filament cross-section x density"),
    AttributeSpec("print_time_s", "float", "s", "derived", "time spent in PRINTING / HIGH_SPEED modes"),
    AttributeSpec("message_rate_hz", "float", "msg/s", "derived", "telemetry messages per second, 5 s window"),
    AttributeSpec("operating_mode", "enum", "", "indicator",
                  "IDLE (<2 mm/s) / PRINTING / HIGH_SPEED (>90 mm/s) from 0.5 s mean feed rate, 3 mm/s hysteresis"),
    AttributeSpec("thermal_state", "enum", "", "indicator",
                  "NORMAL (|T-210|<=5) / WARNING (<=15) / CRITICAL (>15) for filtered nozzle temperature, 1 C hysteresis"),
    AttributeSpec("motor_load", "enum", "", "indicator",
                  "NORMAL (<1.4 A) / HIGH (<1.6 A) / OVERLOAD by 5 s RMS current, 0.02 A hysteresis"),
    AttributeSpec("health_index", "float", "%", "indicator",
                  "100 - thermal - motor - anomaly - data-quality penalties, clamped to 0..100"),
    AttributeSpec("anomalies_total", "int", "", "counter", "outliers detected by the Hampel filter since start"),
    AttributeSpec("anomalies_60s", "int", "", "counter", "outliers detected during the last 60 s"),
    AttributeSpec("data_quality_pct", "float", "%", "counter",
                  "share of good samples among good+outlier+invalid+lost during the last 60 s"),
]
ATTRIBUTE_BY_NAME = {a.name: a for a in ATTRIBUTES}
MEASURED = [m for m in METRICS]
INDICATORS = ["operating_mode", "thermal_state", "motor_load"]


class Quality:
    GOOD = "GOOD"          # value as measured
    FILTERED = "FILTERED"  # outlier replaced by the last accepted value
    STALE = "STALE"        # no fresh data (watchdog)


@dataclass
class ModelEvent:
    type: str        # MODE_CHANGE, THERMAL_STATE, MOTOR_LOAD, CONNECTIVITY, OUTLIER
    severity: str    # info | warning | critical
    attribute: str
    old: object
    new: object
    ts: float        # event time
    message: str


class RollingWindow:
    """Event-time sliding window with O(1) mean / RMS / least-squares slope."""

    def __init__(self, span_s: float):
        self.span = span_s
        self.items: deque[tuple[float, float]] = deque()
        self.t0: float | None = None
        self.n = 0
        self.sv = self.svv = self.st = self.stt = self.stv = 0.0

    def _acc(self, t: float, v: float, sign: int):
        t -= self.t0
        self.n += sign
        self.sv += sign * v
        self.svv += sign * v * v
        self.st += sign * t
        self.stt += sign * t * t
        self.stv += sign * t * v

    def add(self, ts: float, v: float):
        if self.t0 is None:
            self.t0 = ts
        self.items.append((ts, v))
        self._acc(ts, v, +1)
        while self.items and self.items[0][0] < ts - self.span:
            old_t, old_v = self.items.popleft()
            self._acc(old_t, old_v, -1)

    def mean(self) -> float | None:
        return self.sv / self.n if self.n else None

    def rms(self) -> float | None:
        return math.sqrt(max(self.svv, 0.0) / self.n) if self.n else None

    def slope(self) -> float | None:
        den = self.n * self.stt - self.st * self.st
        if self.n < 3 or abs(den) < 1e-9:
            return None
        return (self.n * self.stv - self.st * self.sv) / den


class EventCounter:
    """Counts (timestamped) occurrences within the last span_s seconds."""

    def __init__(self, span_s: float):
        self.span = span_s
        self.items: deque[float] = deque()

    def add(self, t: float, n: int = 1):
        for _ in range(n):
            self.items.append(t)

    def count(self, now: float) -> int:
        while self.items and self.items[0] < now - self.span:
            self.items.popleft()
        return len(self.items)


class DeviceModel:
    """Working (always up to date) state of one printer plus derived-indicator calculators."""

    def __init__(self, printer_id: str, cfg: ModelConfig = MODEL):
        self.cfg = cfg
        self.values: dict[str, object] = {
            "printer_id": printer_id, "connectivity": "UNKNOWN", "last_event_ts": None,
            **{m: None for m in METRICS},
            "nozzle_temp_avg_30s": None, "nozzle_temp_rate": None, "feed_rate_avg_5s": None,
            "stepper_current_rms_5s": None, "extruded_length_mm": 0.0, "filament_used_g": 0.0,
            "print_time_s": 0.0, "message_rate_hz": 0.0,
            "operating_mode": "UNKNOWN", "thermal_state": "UNKNOWN", "motor_load": "UNKNOWN",
            "health_index": None, "anomalies_total": 0, "anomalies_60s": 0, "data_quality_pct": 100.0,
        }
        # per measured attribute: event time and quality flag of the current value
        self.meta: dict[str, dict] = {m: {"ts": None, "quality": None} for m in METRICS}
        self._nozzle_30s = RollingWindow(30.0)
        self._nozzle_10s = RollingWindow(10.0)
        self._feed_05s = RollingWindow(0.5)
        self._feed_5s = RollingWindow(5.0)
        self._current_5s = RollingWindow(5.0)
        self._last_feed: tuple[float, float] | None = None
        self._anomalies = EventCounter(60.0)
        self._good = EventCounter(60.0)
        self._bad = EventCounter(60.0)
        self._arrivals = EventCounter(5.0)

    # --- inputs -------------------------------------------------------------------------------
    def apply_measurement(self, metric: str, ts: float, value: float, filtered: bool) -> list[ModelEvent]:
        v = self.values
        v[metric] = value
        self.meta[metric] = {"ts": ts, "quality": Quality.FILTERED if filtered else Quality.GOOD}
        if v["last_event_ts"] is None or ts > v["last_event_ts"]:
            v["last_event_ts"] = ts
        events: list[ModelEvent] = []

        if metric == "nozzle_temperature":
            self._nozzle_30s.add(ts, value)
            self._nozzle_10s.add(ts, value)
            v["nozzle_temp_avg_30s"] = _r(self._nozzle_30s.mean(), 2)
            v["nozzle_temp_rate"] = _r(self._nozzle_10s.slope(), 3)
            events += self._set_indicator("thermal_state", self._thermal_state(value), ts,
                                          f"nozzle {value:.1f} C (setpoint {self.cfg.nozzle_setpoint:g})")
        elif metric == "extruder_feed_rate":
            self._feed_05s.add(ts, value)
            self._feed_5s.add(ts, value)
            v["feed_rate_avg_5s"] = _r(self._feed_5s.mean(), 2)
            if self._last_feed is not None:
                dt = ts - self._last_feed[0]
                if 0 < dt <= self.cfg.max_integration_gap_s:
                    v["extruded_length_mm"] += 0.5 * (value + self._last_feed[1]) * dt
                    if v["operating_mode"] in ("PRINTING", "HIGH_SPEED"):
                        v["print_time_s"] += dt
            self._last_feed = (ts, value)
            area = math.pi * (self.cfg.filament_diameter_mm / 2) ** 2
            v["filament_used_g"] = v["extruded_length_mm"] * area * self.cfg.filament_density_g_cm3 * 1e-3
            mode = self._operating_mode(self._feed_05s.mean())
            events += self._set_indicator("operating_mode", mode, ts, f"feed rate {self._feed_05s.mean():.1f} mm/s")
        elif metric == "stepper_current_x":
            self._current_5s.add(ts, value)
            rms = self._current_5s.rms()
            v["stepper_current_rms_5s"] = _r(rms, 3)
            events += self._set_indicator("motor_load", self._motor_load(rms), ts, f"X current RMS {rms:.3f} A")
        return events

    def record_outlier(self, metric: str, ts: float, raw: float, median: float, held: float, now: float) -> ModelEvent:
        self.values["anomalies_total"] += 1
        self._anomalies.add(now)
        self._bad.add(now)
        return ModelEvent("OUTLIER", "warning", metric, held, raw, ts,
                          f"{metric}: raw {raw:g} rejected (window median {median:g}), last accepted {held:g} kept")

    def record_quality(self, now: float, good: int = 0, bad: int = 0):
        if good:
            self._good.add(now, good)
        if bad:
            self._bad.add(now, bad)

    def record_arrival(self, now: float):
        self._arrivals.add(now)

    def set_connectivity(self, status: str, ts: float, reason: str = "") -> list[ModelEvent]:
        events = self._set_indicator("connectivity", status, ts, reason or status)
        if status in ("STALE", "OFFLINE"):
            for m, meta in self.meta.items():
                if meta["ts"] is not None:
                    meta["quality"] = Quality.STALE
        return events

    def refresh(self, now: float):
        """Recompute wall-clock based counters and the health index (called on every tick)."""
        v = self.values
        v["anomalies_60s"] = self._anomalies.count(now)
        good, bad = self._good.count(now), self._bad.count(now)
        v["data_quality_pct"] = round(100.0 * good / (good + bad), 1) if good + bad else 100.0
        v["message_rate_hz"] = round(self._arrivals.count(now) / self._arrivals.span, 1)
        v["health_index"] = self._health()

    # --- derived indicators ---------------------------------------------------------------------
    def _set_indicator(self, attr: str, new, ts: float, detail: str) -> list[ModelEvent]:
        old = self.values[attr]
        if new is None or new == old:
            return []
        self.values[attr] = new
        severity = "critical" if new in ("CRITICAL", "OVERLOAD", "OFFLINE") else \
                   "warning" if new in ("WARNING", "HIGH", "STALE") else "info"
        etype = {"operating_mode": "MODE_CHANGE", "thermal_state": "THERMAL_STATE",
                 "motor_load": "MOTOR_LOAD", "connectivity": "CONNECTIVITY"}[attr]
        return [ModelEvent(etype, severity, attr, old, new, ts, f"{attr}: {old} -> {new} ({detail})")]

    def _thermal_state(self, t: float) -> str:
        c = self.cfg
        return _level(abs(t - c.nozzle_setpoint), [c.thermal_warning_delta, c.thermal_critical_delta],
                      ["NORMAL", "WARNING", "CRITICAL"], self.values["thermal_state"], c.thermal_hysteresis)

    def _operating_mode(self, feed: float | None) -> str | None:
        if feed is None:
            return None
        c, h, prev = self.cfg, self.cfg.mode_hysteresis, self.values["operating_mode"]
        if prev == "IDLE" and feed < c.idle_feed_max + h:
            return "IDLE"
        if prev == "HIGH_SPEED" and feed > c.high_speed_feed_min - h:
            return "HIGH_SPEED"
        if feed < c.idle_feed_max:
            return "IDLE"
        return "HIGH_SPEED" if feed > c.high_speed_feed_min else "PRINTING"

    def _motor_load(self, rms: float | None) -> str | None:
        if rms is None:
            return None
        c = self.cfg
        return _level(rms, [c.motor_high_a, c.motor_overload_a], ["NORMAL", "HIGH", "OVERLOAD"],
                      self.values["motor_load"], c.motor_hysteresis)

    def _health(self) -> float:
        v, c = self.values, self.cfg
        h = 100.0
        if v["nozzle_temperature"] is not None:
            h -= min(40.0, 2.0 * max(0.0, abs(v["nozzle_temperature"] - c.nozzle_setpoint) - 2.0))
        if v["stepper_current_rms_5s"] is not None:
            h -= min(30.0, 100.0 * max(0.0, v["stepper_current_rms_5s"] - 1.3))
        h -= min(20.0, 4.0 * v["anomalies_60s"])
        h -= min(10.0, 0.5 * (100.0 - v["data_quality_pct"]))
        if v["connectivity"] in ("STALE", "OFFLINE"):
            h -= 20.0
        return round(max(0.0, min(100.0, h)), 1)


def _level(x: float, thresholds: list[float], names: list[str], prev: str, hysteresis: float) -> str:
    """Alarm level with hysteresis: rises as soon as a threshold is exceeded, falls only when the value
    drops below the threshold minus the hysteresis (no chattering around the threshold)."""
    raw = sum(x > t for t in thresholds)
    cur = names.index(prev) if prev in names else raw
    if raw >= cur:
        return names[raw]
    while cur > raw and x <= thresholds[cur - 1] - hysteresis:
        cur -= 1
    return names[cur]


def _r(x: float | None, nd: int) -> float | None:
    return None if x is None else round(x, nd)
