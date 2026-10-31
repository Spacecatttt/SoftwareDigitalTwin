"""Synchronisation rules: when the working state is committed into the (versioned) twin state.

  PERIODIC  - every `periodic_interval_s` the full working state is committed (bounded staleness,
              heartbeat for subscribers and for the history in the TSDB);
  EVENT     - discrete events are committed immediately: mode / alarm-level changes, connectivity
              changes (birth, LWT, watchdog), detected anomalies;
  THRESHOLD - a measured attribute is committed as soon as it differs from the last committed value
              by at least its deadband (send-on-delta); small fluctuations wait for the periodic commit.
"""
from .config import METRICS, SyncConfig


class SyncReason:
    PERIODIC = "PERIODIC"
    EVENT = "EVENT"
    THRESHOLD = "THRESHOLD"


class SyncPolicy:
    def __init__(self, cfg: SyncConfig):
        self.cfg = cfg
        self.deadbands = {m.name: m.deadband for m in METRICS.values()}
        self.deadbands["health_index"] = cfg.health_deadband

    def threshold_exceeded(self, working: dict, committed: dict, candidates) -> list[str]:
        out = []
        for attr in candidates:
            new = working.get(attr)
            if new is None:
                continue
            old = committed.get(attr)
            if old is None or abs(new - old) >= self.deadbands[attr] - 1e-9:
                out.append(attr)
        return out

    def periodic_due(self, last_periodic: float, now: float) -> bool:
        return now - last_periodic >= self.cfg.periodic_interval_s

    def is_stale(self, last_seen: float, now: float) -> bool:
        return now - last_seen > self.cfg.stale_timeout_s

    @staticmethod
    def event_attributes(event_attr: str) -> list[str]:
        """Attributes committed together with an event (the event attribute + its context)."""
        context = {
            "operating_mode": ["extruder_feed_rate", "feed_rate_avg_5s"],
            "thermal_state": ["nozzle_temperature", "nozzle_temp_rate", "health_index"],
            "motor_load": ["stepper_current_x", "stepper_current_rms_5s", "health_index"],
            "connectivity": ["health_index"],
        }
        return [event_attr] + context.get(event_attr, []) + ["anomalies_total", "anomalies_60s"]
