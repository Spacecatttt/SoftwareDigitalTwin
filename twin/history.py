"""Historical queries to the time-series storage (Flux)."""
import re
from datetime import datetime, timezone

from .config import INFLUX_BUCKET, INFLUX_BUCKET_LONGTERM, INFLUX_ORG, INFLUX_TOKEN, INFLUX_URL

_IDENT = re.compile(r"^[A-Za-z0-9_\-]+$")
_DURATION = re.compile(r"^-?\d+(ms|s|m|h|d|w)$")


def _ident(s: str) -> str:
    if not _IDENT.match(s or ""):
        raise ValueError(f"bad identifier: {s!r}")
    return s


def _range_arg(s: str) -> str:
    """Accepts a Flux duration (-15m), now() or an RFC3339 timestamp."""
    if s in ("now()", "now"):
        return "now()"
    if _DURATION.match(s):
        return s if s.startswith("-") else f"-{s}"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class History:
    def __init__(self, url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, bucket=INFLUX_BUCKET):
        from influxdb_client import InfluxDBClient
        self.client = InfluxDBClient(url=url, token=token, org=org, timeout=30_000)
        self.q = self.client.query_api()
        self.bucket = bucket

    def close(self):
        self.client.close()

    def query(self, flux: str) -> list[dict]:
        out = []
        for table in self.q.query(flux):
            for rec in table.records:
                row = {k: v for k, v in rec.values.items() if k not in ("result", "table")}
                out.append(row)
        return out

    def series(self, printer: str, metric: str, start="-10m", stop="now()", source="resampled",
               every: str | None = None) -> list[dict]:
        """source: resampled (1 s grid with fill method), raw (as measured, with outlier flag) or 1m."""
        p, m, start, stop = _ident(printer), _ident(metric), _range_arg(start), _range_arg(stop)
        if source == "raw":
            flux = f'''from(bucket: "{self.bucket}") |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "telemetry_raw" and r.printer_id == "{p}" and r.metric == "{m}")
  |> filter(fn: (r) => r._field == "value" or r._field == "filtered" or r._field == "outlier" or r._field == "late")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "value", "filtered", "outlier", "late"]) |> sort(columns: ["_time"])'''
        elif source == "1m":
            flux = f'''from(bucket: "{INFLUX_BUCKET_LONGTERM}") |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "telemetry_1m" and r.printer_id == "{p}" and r.metric == "{m}")
  |> keep(columns: ["_time", "_value"]) |> rename(columns: {{_value: "mean"}}) |> sort(columns: ["_time"])'''
        else:
            agg = ""
            if every:
                _ident(every)
                agg = f'|> filter(fn: (r) => r._field == "mean") |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)'
            flux = f'''from(bucket: "{self.bucket}") |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "telemetry_resampled" and r.printer_id == "{p}" and r.metric == "{m}")
  |> filter(fn: (r) => r._field == "mean" or r._field == "fill" or r._field == "count") {agg}
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "mean", "fill", "count"]) |> sort(columns: ["_time"])'''
        return self.query(flux)

    def state_at(self, printer: str, at: str) -> dict:
        """Reconstructs the committed twin state as it was at time `at` (last commit before it)."""
        p = _ident(printer)
        stop = _range_arg(at)
        flux = f'''from(bucket: "{self.bucket}") |> range(start: -30d, stop: {stop})
  |> filter(fn: (r) => r._measurement == "twin_state" and r.printer_id == "{p}")
  |> group(columns: ["_field"]) |> last()'''
        rows = self.query(flux)
        state = {r["_field"]: r["_value"] for r in rows}
        if rows:
            state["_committed_at"] = max(r["_time"] for r in rows).isoformat()
        return state

    def events(self, printer: str | None = None, start="-1h", limit: int = 100) -> list[dict]:
        cond = f' and r.printer_id == "{_ident(printer)}"' if printer else ""
        flux = f'''from(bucket: "{self.bucket}") |> range(start: {_range_arg(start)})
  |> filter(fn: (r) => r._measurement == "twin_events"{cond})
  |> pivot(rowKey: ["_time", "printer_id", "type", "severity"], columnKey: ["_field"], valueColumn: "_value")
  |> group() |> sort(columns: ["_time"], desc: true) |> limit(n: {int(limit)})
  |> keep(columns: ["_time", "printer_id", "type", "severity", "attribute", "old", "new", "message"])'''
        return self.query(flux)

    def fill_stats(self, printer: str, start="-1h") -> list[dict]:
        """How many 1-second buckets of each metric were measured / forward-filled / interpolated / missing."""
        flux = f'''from(bucket: "{self.bucket}") |> range(start: {_range_arg(start)})
  |> filter(fn: (r) => r._measurement == "telemetry_resampled" and r.printer_id == "{_ident(printer)}" and r._field == "fill")
  |> map(fn: (r) => ({{metric: r.metric, fill: r._value, _value: 1}}))
  |> group(columns: ["metric", "fill"]) |> sum() |> group() |> sort(columns: ["metric", "fill"])'''
        return self.query(flux)

    def metric_stats(self, printer: str, start="-1h") -> list[dict]:
        """Aggregates of raw telemetry per metric: count, mean, min, max, number of outliers."""
        p, s = _ident(printer), _range_arg(start)
        base = f'''from(bucket: "{self.bucket}") |> range(start: {s})
  |> filter(fn: (r) => r._measurement == "telemetry_raw" and r.printer_id == "{p}")'''
        flux = f'''data = {base} |> filter(fn: (r) => r._field == "value") |> group(columns: ["metric"])
cnt  = data |> count() |> set(key: "agg", value: "count")
avg  = data |> mean()  |> set(key: "agg", value: "mean")
mn   = data |> min()   |> set(key: "agg", value: "min")
mx   = data |> max()   |> set(key: "agg", value: "max")
outl = {base} |> filter(fn: (r) => r._field == "outlier" and r._value == true) |> group(columns: ["metric"])
  |> count() |> toFloat() |> set(key: "agg", value: "outliers")
union(tables: [cnt |> toFloat(), avg, mn, mx, outl])
  |> keep(columns: ["metric", "agg", "_value"])
  |> pivot(rowKey: ["metric"], columnKey: ["agg"], valueColumn: "_value") |> group() |> sort(columns: ["metric"])'''
        return self.query(flux)

    def pipeline_metrics(self, run_id: str, start="-1h") -> list[dict]:
        flux = f'''from(bucket: "{self.bucket}") |> range(start: {_range_arg(start)})
  |> filter(fn: (r) => r._measurement == "pipeline_metrics" and r.run_id == "{_ident(run_id)}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") |> group() |> sort(columns: ["_time"])'''
        return self.query(flux)
