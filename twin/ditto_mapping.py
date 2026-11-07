"""Mapping of the committed twin state of the pipeline (lab 3 snapshot) onto the Ditto twin graph.

The pipeline keeps one flat state per printer; in Ditto the same state is spread over the twins of the
subsystems. Each row of MAPPING says where an attribute of the pipeline goes: (thing suffix, feature, property).
"""
from datetime import datetime, timezone

from .ditto_graph import printer_thing_id, subsystem_thing_id


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(v, nd=3):
    return None if v is None else round(float(v), nd)


def _int(v):
    return None if v is None else int(v)


def measured(metric: str, field: str = "value"):
    def get(s):
        m = s["measurements"].get(metric) or {}
        if field == "value":
            return _num(m.get("value"))
        if field == "quality":
            return m.get("quality") or "UNKNOWN"
        return iso(m.get("ts"))
    return get


def derived(name: str, nd=3):
    return lambda s: _num(s["derived"].get(name), nd)


def indicator(name: str):
    return lambda s: s["indicators"].get(name)


def counter(name: str, conv=_num):
    return lambda s: conv(s["counters"].get(name))


# (thing suffix: "" = the printer, feature id, property, extractor from the pipeline snapshot)
MAPPING = [
    ("", "status", "operatingMode", indicator("operating_mode")),
    ("", "status", "healthIndex", lambda s: _num(s["indicators"].get("health_index"), 1)),
    ("", "status", "dataQualityPct", counter("data_quality_pct")),
    ("", "status", "anomalies60s", counter("anomalies_60s", _int)),
    ("", "status", "anomaliesTotal", counter("anomalies_total", _int)),
    ("", "status", "printTimeS", derived("print_time_s", 1)),
    ("", "status", "stateVersion", lambda s: _int(s.get("version"))),
    ("", "status", "syncReason", lambda s: s.get("reason")),
    ("", "status", "syncedAt", lambda s: iso(s.get("committed_at"))),

    ("controller", "connectivity", "status", lambda s: s.get("connectivity") or "UNKNOWN"),
    ("controller", "connectivity", "lastEventAt", lambda s: iso(s.get("last_event_ts"))),
    ("controller", "connectivity", "messageRateHz", derived("message_rate_hz", 1)),

    ("hotend", "heater", "temperature", measured("nozzle_temperature")),
    ("hotend", "heater", "temperatureAvg30s", derived("nozzle_temp_avg_30s", 2)),
    ("hotend", "heater", "temperatureRate", derived("nozzle_temp_rate")),
    ("hotend", "heater", "thermalState", indicator("thermal_state")),
    ("hotend", "heater", "quality", measured("nozzle_temperature", "quality")),
    ("hotend", "heater", "measuredAt", measured("nozzle_temperature", "ts")),

    ("bed", "temperature", "temperature", measured("bed_temperature")),
    ("bed", "temperature", "quality", measured("bed_temperature", "quality")),
    ("bed", "temperature", "measuredAt", measured("bed_temperature", "ts")),

    ("enclosure", "temperature", "temperature", measured("chamber_temperature")),
    ("enclosure", "temperature", "quality", measured("chamber_temperature", "quality")),
    ("enclosure", "temperature", "measuredAt", measured("chamber_temperature", "ts")),

    ("extruder", "drive", "feedRate", measured("extruder_feed_rate")),
    ("extruder", "drive", "feedRateAvg5s", derived("feed_rate_avg_5s", 2)),
    ("extruder", "drive", "extrudedLengthMm", derived("extruded_length_mm", 1)),
    ("extruder", "drive", "filamentUsedG", derived("filament_used_g")),
    ("extruder", "drive", "quality", measured("extruder_feed_rate", "quality")),
    ("extruder", "drive", "measuredAt", measured("extruder_feed_rate", "ts")),

    ("axis-x", "motor", "current", measured("stepper_current_x")),
    ("axis-x", "motor", "currentRms5s", derived("stepper_current_rms_5s")),
    ("axis-x", "motor", "loadState", indicator("motor_load")),
    ("axis-x", "motor", "quality", measured("stepper_current_x", "quality")),
    ("axis-x", "motor", "measuredAt", measured("stepper_current_x", "ts")),

    ("fan", "fan", "speedPercent", measured("fan_speed_percent")),
    ("fan", "fan", "quality", measured("fan_speed_percent", "quality")),
    ("fan", "fan", "measuredAt", measured("fan_speed_percent", "ts")),
]


def snapshot_to_properties(snapshot: dict) -> dict[tuple[str, str, str], object]:
    """{(thingId, feature, property): value} for one pipeline snapshot; values that are not known yet (None)
    are skipped - in a JSON merge patch null would delete the property."""
    printer = snapshot["printer_id"]
    out = {}
    for suffix, feature, prop, get in MAPPING:
        value = get(snapshot)
        if value is None:
            continue
        thing_id = subsystem_thing_id(printer, suffix) if suffix else printer_thing_id(printer)
        out[(thing_id, feature, prop)] = value
    return out


def merge_patches(props: dict[tuple[str, str, str], object]) -> dict[str, dict]:
    """Groups property values into one JSON merge patch per Thing: {"features": {f: {"properties": {...}}}}."""
    patches: dict[str, dict] = {}
    for (thing_id, feature, prop), value in props.items():
        features = patches.setdefault(thing_id, {"features": {}})["features"]
        features.setdefault(feature, {"properties": {}})["properties"][prop] = value
    return patches
