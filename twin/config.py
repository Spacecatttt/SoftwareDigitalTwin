"""Static configuration of the pipeline: metric catalogue, preprocessing and sync parameters."""
import os
from dataclasses import dataclass, field

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TELEMETRY_TOPIC = "factory/+/telemetry/#"   # same topic architecture as lab 2
STATUS_TOPIC = "factory/+/status"           # LWT / birth messages from lab 2
TWIN_STATE_TOPIC = "factory/{printer_id}/twin/state"
TWIN_EVENTS_TOPIC = "factory/{printer_id}/twin/events"

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "dt-lab3-token")
INFLUX_ORG = os.getenv("INFLUX_ORG", "kpi")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "digital_twin")              # raw + state, 30 days
INFLUX_BUCKET_LONGTERM = os.getenv("INFLUX_BUCKET_LT", "digital_twin_1m")  # 1-min downsampled, 365 days


@dataclass(frozen=True)
class HampelSpec:
    window: int          # number of past accepted samples
    k: float             # threshold in robust sigmas
    min_sigma: float     # floor for sigma (signal is quantised, MAD may be 0)
    persistence: int = 3 # N consecutive one-sided outliers => level shift, accept


@dataclass(frozen=True)
class MetricSpec:
    name: str
    unit: str
    sensor_id: str
    nominal_hz: float
    valid_min: float     # physical range of the sensor, outside => invalid message
    valid_max: float
    deadband: float      # threshold-of-change sync rule, ~3 sigma of the sensor noise (lab 2 spec: +/-X = 2 sigma)
    hampel: HampelSpec | None
    # gap filling on the resampling grid depends on the physics of the signal: inertial temperatures
    # may be interpolated over long gaps, fast / step-like signals only over short ones
    max_locf_s: float = 5.0
    max_interp_s: float = 10.0


METRICS: dict[str, MetricSpec] = {m.name: m for m in [
    # window of 7 samples: the median of a longer window lags behind slow trends (false outliers)
    MetricSpec("nozzle_temperature", "Celsius", "nozzle_temp_01", 1.0, 0.0, 350.0, 0.75,
               HampelSpec(window=7, k=3.5, min_sigma=0.7)),
    MetricSpec("bed_temperature", "Celsius", "bed_temp_01", 1.0, 0.0, 150.0, 1.5,
               HampelSpec(window=7, k=3.5, min_sigma=0.7)),
    MetricSpec("chamber_temperature", "Celsius", "chamber_temp_01", 1.0, -20.0, 100.0, 1.5,
               HampelSpec(window=7, k=3.5, min_sigma=0.7)),
    MetricSpec("fan_speed_percent", "%", "fan_speed_01", 1.0, 0.0, 100.0, 1.5,
               HampelSpec(window=7, k=3.5, min_sigma=0.7)),
    # Feed rate is a commanded signal with legitimate step changes: range validation only
    MetricSpec("extruder_feed_rate", "mm/s", "feed_rate_01", 10.0, 0.0, 300.0, 3.0, None,
               max_locf_s=1.0, max_interp_s=2.0),
    MetricSpec("stepper_current_x", "A", "current_x_01", 10.0, 0.0, 3.0, 0.05,
               HampelSpec(window=15, k=4.0, min_sigma=0.02), max_locf_s=1.0, max_interp_s=2.0),
]}


@dataclass(frozen=True)
class PipelineConfig:
    reorder_delay_s: float = 0.2      # bounded out-of-orderness of the reorder buffer
    max_future_skew_s: float = 30.0   # timestamps further in the future => invalid
    max_age_s: float = 3600.0         # timestamps older than that => invalid
    dedup_window: int = 2000          # remembered (seq_id, timestamp) keys per stream
    resample_period_s: float = 1.0    # common sampling grid of the analytical layer
    resample_grace_s: float = 0.05
    max_locf_s: float = 5.0           # default: forward-fill gaps up to 5 s (per-metric in MetricSpec) ...
    max_interp_s: float = 10.0        # ... and replace them by linear interpolation when the gap closes
    correction_horizon_s: float = 120.0  # late data within this horizon corrects the resampled series
    missing_markers_s: float = 30.0   # emit explicit "missing" points at most this long, then go dormant


@dataclass(frozen=True)
class SyncConfig:
    periodic_interval_s: float = 1.0  # periodic rule: full snapshot every N seconds
    stale_timeout_s: float = 3.0      # no telemetry for N seconds => connectivity STALE
    health_deadband: float = 5.0      # threshold rule for the derived health index


@dataclass(frozen=True)
class ModelConfig:
    nozzle_setpoint: float = 210.0
    thermal_warning_delta: float = 5.0
    thermal_critical_delta: float = 15.0
    thermal_hysteresis: float = 1.0   # Celsius
    motor_high_a: float = 1.4
    motor_overload_a: float = 1.6
    motor_hysteresis: float = 0.02    # A
    idle_feed_max: float = 2.0        # mm/s
    high_speed_feed_min: float = 90.0 # mm/s
    mode_hysteresis: float = 3.0      # mm/s
    filament_diameter_mm: float = 1.75
    filament_density_g_cm3: float = 1.24  # PLA
    max_integration_gap_s: float = 1.0    # do not integrate feed rate across longer gaps


PIPELINE = PipelineConfig()
SYNC = SyncConfig()
MODEL = ModelConfig()
