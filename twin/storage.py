"""Time-series storage (InfluxDB 2.x): line protocol encoding, batched background writer, schema setup.

Measurements (bucket digital_twin, retention 30 d):
  telemetry_raw        tags: printer_id, metric, sensor_id, unit   fields: value, filtered, seq_id, outlier, late, net_latency_ms
  telemetry_resampled  tags: printer_id, metric                    fields: mean, min, max, count, fill
  twin_state           tags: printer_id, reason                    fields: version + all committed attributes
  twin_events          tags: printer_id, type, severity            fields: attribute, old, new, message
  telemetry_rejected   tags: printer_id, metric, reason            fields: payload
  pipeline_metrics     tags: run_id                                fields: throughput, latency percentiles, ...
Bucket digital_twin_1m (retention 365 d) is filled by the InfluxDB task "downsample_1m".
"""
import logging
import queue
import threading
import time

from .config import INFLUX_BUCKET, INFLUX_BUCKET_LONGTERM, INFLUX_ORG, INFLUX_TOKEN, INFLUX_URL

log = logging.getLogger("twin.storage")

_TAG_ESC = str.maketrans({",": r"\,", "=": r"\=", " ": r"\ "})


def _field(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(v) if v == v and abs(v) != float("inf") else None
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def line(measurement: str, tags: dict, fields: dict, ts: float) -> str | None:
    """Encodes one point in InfluxDB line protocol with microsecond precision."""
    tag_str = "".join(f",{k}={str(v).translate(_TAG_ESC)}" for k, v in tags.items() if v not in (None, ""))
    parts = []
    for k, v in fields.items():
        enc = _field(v)
        if enc is not None:
            parts.append(f"{k.translate(_TAG_ESC)}={enc}")
    if not parts:
        return None
    return f"{measurement}{tag_str} {','.join(parts)} {int(round(ts * 1_000_000))}"


class InfluxWriter(threading.Thread):
    """Background writer: batches lines (by size or time) and posts them; retries with back-off while
    the database is unavailable, keeping at most `max_backlog` lines (oldest are dropped)."""

    def __init__(self, url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, bucket=INFLUX_BUCKET,
                 batch_size=5000, flush_interval=0.5, max_backlog=1_000_000):
        super().__init__(name="influx-writer", daemon=True)
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
        self.client = InfluxDBClient(url=url, token=token, org=org, timeout=30_000)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.bucket, self.org = bucket, org
        self.batch_size, self.flush_interval, self.max_backlog = batch_size, flush_interval, max_backlog
        self.q: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self.lines_written = 0
        self.lines_dropped = 0
        self.batches = 0
        self.errors = 0
        self.last_write_ms = 0.0

    def write(self, ln: str | None):
        if ln is not None:
            self.q.put(ln)

    @property
    def backlog(self) -> int:
        return self.q.qsize()

    def run(self):
        from influxdb_client import WritePrecision
        batch: list[str] = []
        deadline = time.monotonic() + self.flush_interval
        backoff = 0.5
        while not (self._stop_event.is_set() and self.q.empty() and not batch):
            timeout = max(0.0, deadline - time.monotonic())
            try:
                batch.append(self.q.get(timeout=timeout))
                while len(batch) < self.batch_size:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            if batch and (len(batch) >= self.batch_size or time.monotonic() >= deadline or self._stop_event.is_set()):
                t0 = time.perf_counter()
                try:
                    self.write_api.write(self.bucket, self.org, batch, write_precision=WritePrecision.US)
                    self.last_write_ms = (time.perf_counter() - t0) * 1000.0
                    self.lines_written += len(batch)
                    self.batches += 1
                    batch = []
                    backoff = 0.5
                except Exception as exc:
                    self.errors += 1
                    log.warning("[STORAGE] write failed (%s), retry in %.1f s, backlog %d",
                                exc.__class__.__name__, backoff, len(batch) + self.q.qsize())
                    if len(batch) + self.q.qsize() > self.max_backlog:
                        drop = len(batch) // 2
                        self.lines_dropped += drop
                        batch = batch[drop:]
                    if self._stop_event.wait(backoff) and self.errors > 3:
                        break
                    backoff = min(backoff * 2, 10.0)
            if time.monotonic() >= deadline:
                deadline = time.monotonic() + self.flush_interval
        self.client.close()

    def stop(self, timeout=15.0):
        self._stop_event.set()
        self.join(timeout)


DOWNSAMPLE_TASK = f'''option task = {{name: "downsample_1m", every: 1m, offset: 10s}}

from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -task.every)
    |> filter(fn: (r) => r._measurement == "telemetry_resampled" and r._field == "mean")
    |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    |> set(key: "_measurement", value: "telemetry_1m")
    |> to(bucket: "{INFLUX_BUCKET_LONGTERM}", org: "{INFLUX_ORG}")
'''


def ensure_schema(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG):
    """Creates the long-term bucket and the downsampling task if they do not exist yet."""
    from influxdb_client import BucketRetentionRules, InfluxDBClient, TaskCreateRequest, TaskUpdateRequest
    with InfluxDBClient(url=url, token=token, org=org) as client:
        org_id = client.organizations_api().find_organizations(org=org)[0].id
        buckets = client.buckets_api()
        if buckets.find_bucket_by_name(INFLUX_BUCKET_LONGTERM) is None:
            buckets.create_bucket(bucket_name=INFLUX_BUCKET_LONGTERM, org_id=org_id,
                                  retention_rules=BucketRetentionRules(type="expire", every_seconds=365 * 86400))
            log.info("[STORAGE] Created bucket %s (retention 365 d)", INFLUX_BUCKET_LONGTERM)
        # the full script is sent: the "option task" line must precede the query that uses task.every
        # (create_task_every() of the client appends it after the query, and every run then fails)
        tasks = client.tasks_api()
        existing = tasks.find_tasks(name="downsample_1m")
        if not existing:
            tasks.create_task(task_create_request=TaskCreateRequest(flux=DOWNSAMPLE_TASK, org_id=org_id, status="active"))
            log.info("[STORAGE] Created downsampling task downsample_1m")
        elif existing[0].flux.strip() != DOWNSAMPLE_TASK.strip():
            tasks.update_task_request(existing[0].id, TaskUpdateRequest(flux=DOWNSAMPLE_TASK, status="active"))
            log.info("[STORAGE] Updated downsampling task downsample_1m")
