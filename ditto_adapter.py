"""Integration adapter: stream processing pipeline (lab 3) <-> Eclipse Ditto twin graph (lab 4).

  northbound  MQTT factory/+/twin/state (committed twin state published by pipeline.py)
              -> mapping onto the twins of the subsystems (twin/ditto_mapping.py)
              -> only properties that differ from what Ditto already has
              -> coalescing for --flush-ms (the latest value wins)
              -> HTTP PATCH /api/2/things/{thingId}, JSON merge patch, one request per changed Thing
  southbound  Ditto WebSocket: messages to feature inboxes (WoT actions, e.g. emergencyCooldown of the hotend)
              -> MQTT factory/{printer}/commands -> device (emulator.py)

Run:  python ditto_adapter.py          (pipeline.py must be running with the MQTT republisher, the default)
"""
import argparse
import json
import logging
import queue
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import requests

from twin import config
from twin.ditto_client import DittoClient, DittoError, DittoWebSocket, subscription
from twin.ditto_graph import printer_of
from twin.ditto_mapping import merge_patches, snapshot_to_properties
from twin.logutil import setup_logging
from twin.metrics import describe

log = logging.getLogger("adapter")


class Adapter:
    def __init__(self, make_client, flush_s: float, workers: int = 4):
        self.flush_s = flush_s
        # Things are independent: their patches go out in parallel (one HTTP session per worker thread);
        # the patches of one Thing stay ordered because a flush waits for all its requests
        self._local = threading.local()
        self._make_client = make_client
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="ditto-write")
        self.inbox: queue.Queue = queue.Queue()
        self.shadow: dict[tuple, object] = {}    # (thingId, feature, property) -> value confirmed by Ditto
        self.pending: dict[tuple, object] = {}   # changes waiting for the next flush
        self.not_provisioned: set[str] = set()
        self.stats = {"states": 0, "patches": 0, "properties": 0, "unchanged": 0, "rejected": 0, "errors": 0}
        self.write_ms: list[float] = []
        self._stop_event = threading.Event()

    # --- MQTT thread -------------------------------------------------------------------------------------
    def on_state(self, payload: bytes):
        try:
            msg = json.loads(payload)
            snapshot = msg.get("snapshot", msg)
            if snapshot.get("printer_id"):
                self.inbox.put(snapshot)
        except ValueError:
            log.warning("[ADAPTER] not a JSON twin state: %.80r", payload)

    # --- worker thread -----------------------------------------------------------------------------------
    def run(self):
        next_flush = time.monotonic() + self.flush_s
        backoff = 0.0
        while not self._stop_event.is_set():
            try:
                self.apply(self.inbox.get(timeout=max(0.0, next_flush - time.monotonic())))
                continue
            except queue.Empty:
                pass
            ok = self.flush()
            backoff = 0.0 if ok else min(max(backoff * 2, 0.5), 10.0)
            next_flush = time.monotonic() + max(self.flush_s, backoff)

    def apply(self, snapshot: dict):
        self.stats["states"] += 1
        for key, value in snapshot_to_properties(snapshot).items():
            if key[0] in self.not_provisioned:
                continue
            if self.shadow.get(key) == value:
                self.pending.pop(key, None)    # changed and changed back before the flush
                self.stats["unchanged"] += 1
            else:
                self.pending[key] = value

    def flush(self) -> bool:
        """Sends one merge patch per Thing with pending changes; returns False if Ditto is unreachable."""
        if not self.pending:
            return True
        batch, self.pending = self.pending, {}
        patches = merge_patches(batch)
        results = dict(zip(patches, self.pool.map(self._send, patches.items())))
        reachable = True
        for thing_id, (t_ms, e) in results.items():
            keys = [k for k in batch if k[0] == thing_id]
            if isinstance(e, DittoError):
                if e.status in (404, 412) or e.json.get("error") == "entity.notcreatable":
                    self.not_provisioned.add(thing_id)
                    log.warning("[ADAPTER] %s does not exist in Ditto - run: python ditto_setup.py %s",
                                thing_id, printer_of(thing_id))
                else:   # 400 = the value violates the WoT Thing Model, 403 = not allowed by the policy
                    self.stats["rejected"] += 1
                    log.warning("[ADAPTER] %s rejected: %s %s", thing_id, e.status,
                                e.json.get("description") or e.body[:200])
                continue
            if e is not None:                       # Ditto unreachable: keep the changes, newer values win
                self.stats["errors"] += 1
                for k in keys:
                    self.pending.setdefault(k, batch[k])
                reachable = False
                continue
            self.write_ms.append(t_ms)
            self.stats["patches"] += 1
            self.stats["properties"] += len(keys)
            for k in keys:
                self.shadow[k] = batch[k]
        if not reachable:
            log.warning("[ADAPTER] Ditto unreachable, %d properties kept for retry", len(self.pending))
        return reachable

    def _send(self, item) -> tuple[float, Exception | None]:
        thing_id, patch = item
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._local.client = self._make_client()
        t0 = time.monotonic_ns()
        try:
            # the send time travels with the change: Ditto copies the correlation-id into the twin event,
            # so an event consumer (ditto_monitor.py) can measure the delivery latency on the same host
            # If-Match: * - update only an existing twin: a JSON merge patch on a missing Thing would create it
            client.merge_thing(thing_id, patch, correlation_id=f"adapter:{t0}:{uuid.uuid4().hex[:6]}", if_match="*")
        except (DittoError, requests.RequestException) as e:
            return 0.0, e
        return (time.monotonic_ns() - t0) / 1e6, None

    def report(self, interval: float):
        w, self.write_ms = self.write_ms, []
        d = describe(w)
        s, self.stats = self.stats, dict.fromkeys(self.stats, 0)
        log.info("[ADAPTER] states %.1f/s | Ditto writes %.1f/s (%d properties, %d unchanged skipped) | "
                 "write ms mean %s max %s | pending %d | rejected %d errors %d",
                 s["states"] / interval, s["patches"] / interval, s["properties"], s["unchanged"],
                 _f(d["mean"]), _f(d["max"]), len(self.pending), s["rejected"], s["errors"])

    def stop(self):
        self._stop_event.set()


class CommandBridge:
    """Ditto feature messages (WoT actions) -> MQTT commands to the device."""

    def __init__(self, client: mqtt.Client):
        self.client = client

    def on_ditto_message(self, msg: dict):
        topic, path = msg.get("topic", ""), msg.get("path", "")
        if "/things/live/messages/" not in topic or "/inbox/messages/" not in path:
            return
        namespace, name = topic.split("/")[:2]
        thing_id = f"{namespace}:{name}"
        subject = path.rsplit("/", 1)[-1]
        feature = path.split("/")[2] if path.startswith("/features/") else None
        printer = printer_of(thing_id)
        command = {"command": subject, "thingId": thing_id, "feature": feature, "args": msg.get("value"),
                   "correlationId": msg.get("headers", {}).get("correlation-id"),
                   "issuedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        self.client.publish(config.COMMANDS_TOPIC.format(printer_id=printer), json.dumps(command), qos=1)
        log.warning("[COMMAND] Ditto %s %s -> MQTT %s", thing_id, path, config.COMMANDS_TOPIC.format(printer_id=printer))


def _f(v):
    return "-" if v is None else f"{v:.1f}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mqtt-host", default=config.MQTT_HOST)
    p.add_argument("--mqtt-port", type=int, default=config.MQTT_PORT)
    p.add_argument("--ditto-url", default=config.DITTO_URL)
    p.add_argument("--user", default="adapter")
    p.add_argument("--password", default=None, help="default: same as the user")
    p.add_argument("--flush-ms", type=float, default=250, help="coalescing interval of Ditto writes")
    p.add_argument("--workers", type=int, default=4, help="parallel HTTP requests to Ditto")
    p.add_argument("--report-s", type=float, default=10, help="statistics interval")
    p.add_argument("--no-commands", action="store_true", help="do not forward Ditto messages to the devices")
    p.add_argument("--log-file", default="logs/ditto_adapter.log")
    args = p.parse_args()
    setup_logging(args.log_file)

    DittoClient(args.user, args.password, url=args.ditto_url).wait_ready()
    adapter = Adapter(lambda: DittoClient(args.user, args.password, url=args.ditto_url), args.flush_ms / 1000,
                      args.workers)
    worker = threading.Thread(target=adapter.run, name="ditto-writer", daemon=True)
    worker.start()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, f"DittoAdapter_{uuid.uuid4().hex[:6]}")

    def on_connect(c, userdata, flags, reason_code, properties=None):
        log.info("[MQTT] connected to %s:%d (%s)", args.mqtt_host, args.mqtt_port, reason_code)
        c.subscribe(config.TWIN_STATE_TOPIC.format(printer_id="+"), qos=0)

    client.on_connect = on_connect
    client.on_message = lambda c, u, m: adapter.on_state(m.payload)
    client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=30)
    client.loop_start()

    ws = None
    if not args.no_commands:
        bridge = CommandBridge(client)
        ws = DittoWebSocket(args.user, args.password,
                            [subscription("START-SEND-MESSAGES", namespaces=config.DITTO_NAMESPACE)],
                            bridge.on_ditto_message, url=args.ditto_url, name="ditto-commands")
        ws.start()

    log.info("[ADAPTER] %s -> Ditto %s as '%s', flush %.0f ms%s", config.TWIN_STATE_TOPIC.format(printer_id="+"),
             args.ditto_url, args.user, args.flush_ms, "" if args.no_commands else ", commands Ditto -> MQTT on")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    last = time.monotonic()
    while not stop.wait(args.report_s):
        now = time.monotonic()
        adapter.report(now - last)
        last = now
    adapter.stop()
    worker.join(2)
    adapter.flush()
    if ws:
        ws.stop()
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
