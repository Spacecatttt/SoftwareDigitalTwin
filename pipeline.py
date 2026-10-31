"""Real-time stream processing pipeline and digital twin state synchronisation.

  MQTT -> buffer -> validation / dedup -> reorder buffer -> Hampel filter ->
  state update + derived indicators -> sync rules (periodic / event / threshold) -> twin state store ->
  subscribers (console, MQTT twin topics, web dashboard via SSE, InfluxDB history)
                                             \\-> resampling to 1 s grid + gap filling -> InfluxDB

Run:  python pipeline.py            (dashboard on http://localhost:8050)
"""

import argparse
import csv
import json
import logging
import signal
import threading
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import paho.mqtt.client as mqtt

from twin import config
from twin.processor import StreamProcessor
from twin.state_store import Notification, QueueSubscriber
from twin.web import DashboardServer, notification_json

log = logging.getLogger("pipeline")


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--mqtt-host", default=config.MQTT_HOST)
    p.add_argument("--mqtt-port", type=int, default=config.MQTT_PORT)
    p.add_argument("--client-id", default="DigitalTwin_Pipeline")
    p.add_argument(
        "--clean-session",
        action="store_true",
        help="do not resume the persistent MQTT session",
    )
    p.add_argument("--no-storage", action="store_true", help="do not write to InfluxDB")
    p.add_argument(
        "--web-port", type=int, default=8050, help="0 disables the dashboard"
    )
    p.add_argument(
        "--no-republish", action="store_true", help="do not publish twin state to MQTT"
    )
    p.add_argument(
        "--reorder-ms", type=float, default=config.PIPELINE.reorder_delay_s * 1000
    )
    p.add_argument(
        "--resample-s", type=float, default=config.PIPELINE.resample_period_s
    )
    p.add_argument("--periodic-s", type=float, default=config.SYNC.periodic_interval_s)
    p.add_argument("--stale-s", type=float, default=config.SYNC.stale_timeout_s)
    p.add_argument("--run-id", default="live")
    p.add_argument(
        "--duration",
        type=float,
        default=0,
        help="stop after N seconds (0 = until Ctrl+C)",
    )
    p.add_argument("--metrics-csv", help="write per-second pipeline metrics to CSV")
    p.add_argument(
        "--summary-json",
        help="write run summary (latency percentiles, counters) to JSON",
    )
    p.add_argument(
        "--latency-samples",
        help="write every measured latency (net, proc, e2e in ms) to CSV",
    )
    p.add_argument(
        "--quiet", action="store_true", help="log only events and per-second metrics"
    )
    p.add_argument("--log-file", default="logs/pipeline.log")
    return p.parse_args()


class SteadyTimeFilter(logging.Filter):
    """Log timestamps from start offset + monotonic clock: the WSL2 wall clock is stepped back and forth
    by time synchronisation, which would make the log go backwards in time."""

    offset = time.time() - time.monotonic()

    def filter(self, record):
        record.created = self.offset + time.monotonic()
        record.msecs = (record.created % 1) * 1000
        return True


def setup_logging(args):
    Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.FileHandler(args.log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    for h in handlers:
        h.addFilter(SteadyTimeFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    logging.getLogger("influxdb_client").setLevel(logging.WARNING)


class ConsoleNotifier:
    """Subscriber that prints committed twin state changes."""

    def __init__(self, quiet: bool):
        self.quiet = quiet

    def __call__(self, n: Notification):
        if n.events:
            for e in n.events:
                level = logging.WARNING if e["severity"] != "info" else logging.INFO
                log.log(
                    level,
                    "[TWIN %s v%d EVENT] %s",
                    n.printer_id,
                    n.version,
                    e["message"],
                )
        elif not self.quiet and n.reason == "THRESHOLD":
            ch = ", ".join(
                f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                for k, v in n.changes.items()
            )
            log.info(
                "[TWIN %s v%d THRESHOLD] %s (latency %.1f ms)",
                n.printer_id,
                n.version,
                ch,
                n.latency_ms or 0,
            )


class MqttRepublisher(threading.Thread):
    """Subscriber that publishes the twin state to MQTT so that any remote client can follow it:
    factory/{id}/twin/state (retained full snapshot) and factory/{id}/twin/events."""

    def __init__(self, client: mqtt.Client):
        super().__init__(name="mqtt-republisher", daemon=True)
        self.client, self.sub = client, QueueSubscriber(maxsize=5000)
        self._last_state: dict[str, float] = {}

    def run(self):
        while True:
            n = self.sub.q.get()
            if n is None:
                return
            for e in n.events:
                self.client.publish(
                    config.TWIN_EVENTS_TOPIC.format(printer_id=n.printer_id),
                    json.dumps(e, default=str),
                    qos=1,
                )
            # full snapshot at most 10 times per second per device (latest state wins)
            if (
                n.events
                or n.reason == "PERIODIC"
                or n.commit_ts - self._last_state.get(n.printer_id, 0) >= 0.1
            ):
                self._last_state[n.printer_id] = n.commit_ts
                self.client.publish(
                    config.TWIN_STATE_TOPIC.format(printer_id=n.printer_id),
                    json.dumps(notification_json(n), default=str),
                    qos=0,
                    retain=True,
                )


def main():
    args = parse_args()
    setup_logging(args)
    pcfg = replace(
        config.PIPELINE,
        reorder_delay_s=args.reorder_ms / 1000.0,
        resample_period_s=args.resample_s,
    )
    scfg = replace(
        config.SYNC, periodic_interval_s=args.periodic_s, stale_timeout_s=args.stale_s
    )

    writer = history = None
    if not args.no_storage:
        from twin.history import History
        from twin.storage import InfluxWriter, ensure_schema

        try:
            ensure_schema()
        except Exception as exc:
            log.warning("[STORAGE] schema setup skipped: %s", exc)
        writer = InfluxWriter()
        writer.start()
        history = History()

    proc = StreamProcessor(
        pcfg, scfg, sink=writer.write if writer else None, run_id=args.run_id
    )
    if writer:
        proc.gauges = lambda: {
            "writer_backlog": writer.backlog,
            "lines_written": writer.lines_written,
            "write_ms": round(writer.last_write_ms, 2),
            "write_errors": writer.errors,
        }
    proc.store.subscribe(ConsoleNotifier(args.quiet))

    csv_file = csv_writer = None
    if args.metrics_csv:
        Path(args.metrics_csv).parent.mkdir(parents=True, exist_ok=True)
        csv_file = open(args.metrics_csv, "w", newline="")

    def on_metrics(row):
        nonlocal csv_writer
        if csv_file:
            if csv_writer is None:
                csv_writer = csv.DictWriter(
                    csv_file, fieldnames=list(row), extrasaction="ignore"
                )
                csv_writer.writeheader()
            csv_writer.writerow(row)
            csv_file.flush()
        if row["processed"] or row["received"]:
            log.info(
                "[METRICS] in %4d msg/s | processed %4d msg/s | e2e mean %6.1f ms  p95 %6.1f  max %7.1f | "
                "inbox %d | invalid %d dup %d late %d lost %d outliers %d | commits P/E/T %d/%d/%d",
                row["ingress_mps"],
                row["throughput_mps"],
                row["e2e_mean_ms"] or 0,
                row["e2e_p95_ms"] or 0,
                row["e2e_max_ms"] or 0,
                row["inbox_depth"],
                row["invalid"],
                row["duplicate"],
                row["late"],
                row["lost"],
                row["outlier"],
                row["commits_periodic"],
                row["commits_event"],
                row["commits_threshold"],
            )

    proc.metric_rows.append(on_metrics)

    client_id = (
        args.client_id
        if not args.clean_session
        else f"{args.client_id}_{uuid.uuid4().hex[:6]}"
    )
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id, clean_session=args.clean_session
    )
    client.max_inflight_messages_set(1000)
    client.reconnect_delay_set(1, 10)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info(
                "[MQTT] Connected to %s:%d as %s (session present: %s)",
                args.mqtt_host,
                args.mqtt_port,
                client_id,
                flags.session_present,
            )
            c.subscribe([(config.TELEMETRY_TOPIC, 1), (config.STATUS_TOPIC, 1)])
        else:
            log.error("[MQTT] Connection failed: %s", reason_code)

    def on_disconnect(c, userdata, flags, reason_code, properties=None):
        log.warning("[MQTT] Disconnected (%s), reconnecting...", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = lambda c, u, msg: proc.submit(msg.topic, msg.payload)

    if not args.no_republish:
        republisher = MqttRepublisher(client)
        proc.store.subscribe(republisher.sub)
        republisher.start()

    web = None
    if args.web_port:
        web = DashboardServer(proc.store, args.web_port, history)
        proc.metric_rows.append(web.on_metrics)
        web.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())

    client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=30)
    client.loop_start()
    log.info(
        "[PIPELINE] Started: reorder %.0f ms, resample %.2f s, periodic sync %.2f s, stale %.1f s, storage %s",
        args.reorder_ms,
        args.resample_s,
        args.periodic_s,
        args.stale_s,
        "on" if writer else "off",
    )
    t_end = time.time() + args.duration if args.duration else float("inf")
    try:
        while not stop.is_set() and time.time() < t_end:
            proc.run_once()
    finally:
        log.info("[PIPELINE] Stopping...")
        client.loop_stop()
        client.disconnect()
        proc.drain()
        if writer:
            writer.stop()
        if web:
            web.stop()
        if csv_file:
            csv_file.close()
        summary = {
            "run_id": args.run_id,
            "config": {"pipeline": asdict(pcfg), "sync": asdict(scfg)},
            **proc.metrics.summary(),
            "commit_counts": proc.store.commit_counts,
            "resample_dropped_too_late": proc.resampler.dropped_too_late,
            "writer": {
                "lines_written": writer.lines_written,
                "batches": writer.batches,
                "errors": writer.errors,
                "dropped": writer.lines_dropped,
            }
            if writer
            else None,
            "devices": proc.store.snapshots(),
        }
        if args.summary_json:
            Path(args.summary_json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary_json).write_text(
                json.dumps(summary, indent=2, default=str)
            )
        if args.latency_samples:
            m = proc.metrics
            with open(args.latency_samples, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["net_ms", "proc_ms", "e2e_ms"])
                w.writerows(
                    zip(
                        (round(v, 3) for v in m.run_net),
                        (round(v, 3) for v in m.run_proc),
                        (round(v, 3) for v in m.run_e2e),
                    )
                )
        e2e = summary["e2e_ms"]
        if e2e["n"]:
            log.info(
                "[PIPELINE] processed %d measurements, e2e latency mean %.1f ms, p95 %.1f ms, max %.1f ms",
                e2e["n"],
                e2e["mean"],
                e2e["p95"],
                e2e["max"],
            )


if __name__ == "__main__":
    main()
