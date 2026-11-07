"""Event monitor: an external component that reacts to change notifications of the Ditto twins.

Subscription (WebSocket, Ditto Protocol), filtered by Ditto itself:
  START-SEND-EVENTS?namespaces=kpi.dt
    &filter=or(exists(features/heater/properties/thermalState),exists(features/connectivity/properties/status))
    &extraFields=attributes/kind,attributes/relations,features/heater/properties/temperature
The filter is applied to the changed data of an event, so the monitor gets an event only when the thermal state of a
hotend or the link state of a controller changes. Reactions:
  hotend CRITICAL      -> alarm on the printer twin + WoT action emergencyCooldown of the hotend (Ditto message,
                          forwarded to the device by ditto_adapter.py)
  hotend WARNING       -> alarm WARNING
  hotend NORMAL        -> alarm cleared
  controller OFFLINE / STALE -> alarm WARNING, ONLINE -> cleared
  after every reaction -> roll-up of the farm (features/summary of kpi.dt:farm-01) from search counts

Run:  python ditto_monitor.py                  (--no-react: only print events, --filter '' : all events)
"""
import argparse
import logging
import queue
import signal
import threading
import time
from datetime import datetime, timezone

from twin import config
from twin.ditto_client import DittoClient, DittoError, DittoWebSocket, subscription
from twin.ditto_graph import FARM_ID
from twin.logutil import setup_logging
from twin.metrics import describe

log = logging.getLogger("monitor")

DEFAULT_FILTER = "or(exists(features/heater/properties/thermalState),exists(features/connectivity/properties/status))"
EXTRA_FIELDS = "attributes/kind,attributes/relations,features/heater/properties/temperature"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def changed_values(event: dict) -> dict:
    """Flattens the changed data of a Ditto event into {"/features/heater/properties/thermalState": value}."""
    out = {}

    def walk(prefix, v):
        if isinstance(v, dict) and v:
            for k, sub in v.items():
                walk(f"{prefix}/{k}", sub)
        else:
            out[prefix or "/"] = v

    walk(event.get("path", "/").rstrip("/"), event.get("value"))
    return out


def delivery_latency_ms(event: dict) -> float | None:
    """The adapter puts its monotonic send time into the correlation-id; Ditto copies it into the event."""
    cid = event.get("headers", {}).get("correlation-id", "")
    if cid.startswith("adapter:"):
        try:
            return (time.monotonic_ns() - int(cid.split(":")[1])) / 1e6
        except (IndexError, ValueError):
            return None
    return None


class Monitor:
    def __init__(self, ditto: DittoClient, react: bool):
        self.ditto, self.react = ditto, react
        self.latency: list[float] = []
        self.events = 0
        self._summary_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        # reactions call the Ditto API; they run in their own thread so that a slow call never delays the
        # reception of the next event on the WebSocket
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._work, name="reactions", daemon=True).start()

    def _work(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except DittoError as e:
                log.warning("[REACTION] failed: %s", e)

    def on_event(self, ev: dict):
        topic = ev.get("topic", "")
        if "/things/twin/events/" not in topic:
            return
        namespace, name = topic.split("/")[:2]
        thing_id = f"{namespace}:{name}"
        lat = delivery_latency_ms(ev)
        with self._lock:
            self.events += 1
            if lat is not None:
                self.latency.append(lat)
        extra = ev.get("extra", {})
        kind = extra.get("attributes", {}).get("kind")
        relations = extra.get("attributes", {}).get("relations", {})
        values = changed_values(ev)
        lat_s = f"{lat:.0f} ms" if lat is not None else "-"
        log.info("[EVENT] %s rev %s %s by %s, delivered in %s", thing_id, ev.get("revision"),
                 ", ".join(f"{k.split('/')[-1]}={v}" for k, v in values.items()),
                 ev.get("headers", {}).get("ditto-originator", "?"), lat_s)
        if not self.react:
            return
        printer = relations.get("isPartOf")
        thermal = values.get("/features/heater/properties/thermalState")
        link = values.get("/features/connectivity/properties/status")
        if kind == "Hotend" and thermal and printer:
            temp = extra.get("features", {}).get("heater", {}).get("properties", {}).get("temperature")
            self.jobs.put(lambda: self.on_thermal(thing_id, printer, thermal, temp))
        if kind == "Controller" and link and printer:
            self.jobs.put(lambda: self.on_link(thing_id, printer, link))

    # --- reactions ----------------------------------------------------------------------------------
    def on_thermal(self, hotend: str, printer: str, state: str, temp):
        t = f"{temp:.1f} C" if isinstance(temp, (int, float)) else "?"
        if state == "CRITICAL":
            # safety first: the action goes out before any bookkeeping. Ditto delivers the message at once;
            # the HTTP answer of a fire-and-forget request (timeout=0) comes after the enforcement check (<= 3 s)
            log.warning("[REACTION] %s: CRITICAL %s -> action emergencyCooldown of %s", printer, t, hotend)
            threading.Thread(target=self._send_action, daemon=True,
                             args=(hotend, "emergencyCooldown", {"reason": f"thermalState CRITICAL at {t}"})).start()
            self.raise_alarm(printer, "CRITICAL", f"Hotend overheating: {t}", hotend)
        elif state == "WARNING":
            self.raise_alarm(printer, "WARNING", f"Hotend temperature out of band: {t}", hotend)
        elif state == "NORMAL":
            self.clear_alarm(printer, hotend, f"hotend back to NORMAL ({t})")

    def _send_action(self, hotend: str, action: str, payload: dict):
        try:
            self.ditto.send_feature_message(hotend, "heater", action, payload)
        except DittoError as e:   # e.g. 400 if the payload violates the WoT action input, 403 by the policy
            log.error("[REACTION] action %s of %s rejected: %s", action, hotend, e)

    def on_link(self, controller: str, printer: str, status: str):
        if status in ("OFFLINE", "STALE"):
            self.raise_alarm(printer, "WARNING", f"Printer {status}", controller)
        elif status == "ONLINE":
            self.clear_alarm(printer, controller, "printer back ONLINE")

    def raise_alarm(self, printer: str, level: str, message: str, source: str):
        cur = (self.ditto.get_thing(printer, fields="features/alarms/properties") or {}) \
            .get("features", {}).get("alarms", {}).get("properties", {})
        count = cur.get("count", 0) + (0 if cur.get("active") and cur.get("source") == source else 1)
        self.ditto.merge_thing(printer, {"features": {"alarms": {"properties": {
            "active": True, "level": level, "message": message, "source": source, "raisedAt": now_iso(),
            "count": count}}}})
        log.warning("[REACTION] alarm %s on %s: %s", level, printer, message)
        self.schedule_summary()

    def clear_alarm(self, printer: str, source: str, why: str):
        cur = (self.ditto.get_thing(printer, fields="features/alarms/properties") or {}) \
            .get("features", {}).get("alarms", {}).get("properties", {})
        if not cur.get("active") or cur.get("source") != source:
            return
        self.ditto.merge_thing(printer, {"features": {"alarms": {"properties": {"active": False, "level": "NONE"}}}})
        log.info("[REACTION] alarm cleared on %s: %s", printer, why)
        self.schedule_summary()

    def schedule_summary(self, delay_s: float = 1.5):
        """The search index is eventually consistent (updated ~1 s after a change): debounce the roll-up."""
        if self._summary_timer:
            self._summary_timer.cancel()
        self._summary_timer = threading.Timer(delay_s, self.update_summary)
        self._summary_timer.daemon = True
        self._summary_timer.start()

    def update_summary(self):
        try:
            printers = f'eq(attributes/kind,"Printer")'
            summary = {
                "printersTotal": self.ditto.count(printers),
                "printersOnline": self.ditto.count('and(eq(attributes/kind,"Controller"),'
                                                   'eq(features/connectivity/properties/status,"ONLINE"))'),
                "printersInAlarm": self.ditto.count(f"and({printers},eq(features/alarms/properties/active,true))"),
                "updatedAt": now_iso(),
            }
            self.ditto.merge_thing(FARM_ID, {"features": {"summary": {"properties": summary}}})
            log.info("[REACTION] %s summary: %d printers, %d online, %d in alarm", FARM_ID,
                     summary["printersTotal"], summary["printersOnline"], summary["printersInAlarm"])
        except DittoError as e:
            log.warning("[REACTION] summary update failed: %s", e)

    def report(self):
        with self._lock:
            lat, self.latency = self.latency, []
            n, self.events = self.events, 0
        if n:
            d = describe(lat)
            log.info("[MONITOR] %d events | delivery latency adapter -> Ditto -> monitor: mean %s, p95 %s, max %s ms",
                     n, *(f"{d[k]:.0f}" if d[k] is not None else "-" for k in ("mean", "p95", "max")))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ditto-url", default=config.DITTO_URL)
    p.add_argument("--user", default="monitor")
    p.add_argument("--password", default=None)
    p.add_argument("--filter", default=DEFAULT_FILTER, help="RQL filter of the subscription ('' = all events)")
    p.add_argument("--no-react", action="store_true", help="only print the events")
    p.add_argument("--report-s", type=float, default=30)
    p.add_argument("--log-file", default="logs/ditto_monitor.log")
    args = p.parse_args()
    setup_logging(args.log_file)

    ditto = DittoClient(args.user, args.password, url=args.ditto_url)
    ditto.wait_ready()
    monitor = Monitor(ditto, react=not args.no_react)
    sub = subscription("START-SEND-EVENTS", namespaces=config.DITTO_NAMESPACE, filter=args.filter,
                       extraFields=EXTRA_FIELDS)
    log.info("[MONITOR] subscribing as '%s': %s", args.user, sub)
    ws = DittoWebSocket(args.user, args.password, [sub], monitor.on_event, url=args.ditto_url, name="ditto-events")
    ws.start()
    if monitor.react:
        monitor.update_summary()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    while not stop.wait(args.report_s):
        monitor.report()
    monitor.report()
    ws.stop()


if __name__ == "__main__":
    main()
