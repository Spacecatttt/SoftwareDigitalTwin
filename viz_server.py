"""Web backend of the 3D digital twin viewer (lab 5): backend-for-frontend between the browser and the platform.

  browser (web/3d, Three.js) ──HTTP / SSE──▶ viz_server.py ──WebSocket, Ditto Protocol (user viewer)──▶ Eclipse Ditto
                                                           └─Flux──────────────────────────────────────▶ InfluxDB (lab 3)
  GET /3d/                  the 3D app (static files of web/3d)
  GET /api/stream           server-sent events: "snapshot" (all twins), then "twin" (every change of a twin), "status"
  GET /api/history/series   time series of a metric from the time-series storage of lab 3
  GET /api/history/events   twin events (alarm transitions) from the storage

The Ditto credentials stay on the server; the viewer user may only read the twins (policy entry "viewer").
Run:  python viz_server.py        then open http://localhost:8060/3d/
"""
import argparse
import json
import logging
import mimetypes
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from twin import config
from twin.ditto_client import DittoClient, DittoWebSocket, subscription
from twin.logutil import setup_logging

log = logging.getLogger("viz")
WEB_3D = Path(__file__).resolve().parent / "web" / "3d"
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("model/gltf-binary", ".glb")


class TwinRelay:
    """One upstream subscription to Ditto, fanned out to every connected browser (bounded queue per client,
    the oldest change is dropped if a browser cannot keep up)."""

    def __init__(self, user: str, password: str | None, url: str):
        self.ditto = DittoClient(user, password, url=url)
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.events = 0
        self.ws = DittoWebSocket(user, password,
                                 [subscription("START-SEND-EVENTS", namespaces=config.DITTO_NAMESPACE)],
                                 self.on_event, url=url, name="ditto-viewer")

    def start(self):
        self.ws.start()

    def snapshot(self) -> list[dict]:
        return self.ditto.search(namespaces=config.DITTO_NAMESPACE)

    def on_event(self, ev: dict):
        topic = ev.get("topic", "")
        if "/things/twin/events/" not in topic:
            return
        namespace, name = topic.split("/")[:2]
        msg = {"thingId": f"{namespace}:{name}", "action": topic.rsplit("/", 1)[-1], "path": ev.get("path", "/"),
               "value": ev.get("value"), "revision": ev.get("revision"), "timestamp": ev.get("timestamp")}
        cid = ev.get("headers", {}).get("correlation-id", "")
        if cid.startswith("adapter:"):    # adapter send time (monotonic, same host) -> delivery latency
            try:
                msg["latencyMs"] = round((time.monotonic_ns() - int(cid.split(":")[1])) / 1e6, 1)
            except ValueError:
                pass
        self.events += 1
        with self.lock:
            for q in self.clients:
                _put(q, ("twin", msg))

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


def _put(q: queue.Queue, item):
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


def make_handler(relay: TwinRelay, history):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8", cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/3d"):
                    self.send_response(302)
                    self.send_header("Location", "/3d/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif url.path.startswith("/3d/"):
                    self._static(url.path[len("/3d/"):] or "index.html")
                elif url.path == "/api/stream":
                    self._stream()
                elif url.path.startswith("/api/history/") and history is None:
                    self._json({"error": "storage disabled"}, 503)
                elif url.path == "/api/history/series":
                    self._json(history.series(qs["printer"], qs["metric"], qs.get("start", "-15m"),
                                              qs.get("stop", "now()"), qs.get("source", "resampled"), qs.get("every")))
                elif url.path == "/api/history/events":
                    self._json(history.events(qs.get("printer"), qs.get("start", "-1h"), int(qs.get("limit", 50))))
                else:
                    self._json({"error": "not found"}, 404)
            except (KeyError, ValueError) as exc:
                self._json({"error": str(exc)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                log.exception("[VIZ] request failed")
                self._json({"error": str(exc)}, 500)

        def _static(self, rel: str):
            path = (WEB_3D / rel).resolve()
            if WEB_3D not in path.parents or not path.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            # vendor libraries and the model are immutable within a release; the app itself is not cached
            cache = "max-age=3600" if "vendor" in path.parts or path.suffix == ".glb" else "no-store"
            self._send(200, path.read_bytes(), ctype, cache)

        def _event(self, name: str, data):
            self.wfile.write(f"event: {name}\ndata: ".encode() + json.dumps(data, default=str).encode() + b"\n\n")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = relay.subscribe()            # subscribe before the snapshot: no change is lost in between
            try:
                self._event("snapshot", relay.snapshot())
                self.wfile.flush()
                last_status = 0.0
                while True:
                    try:
                        name, data = q.get(timeout=1.0)
                        self._event(name, data)
                        while not q.empty():
                            name, data = q.get_nowait()
                            self._event(name, data)
                    except queue.Empty:
                        pass
                    if time.monotonic() - last_status >= 1.0:
                        last_status = time.monotonic()
                        self._event("status", {"ditto": relay.ws.connected.is_set(), "events": relay.events})
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                relay.unsubscribe(q)

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8060)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--ditto-url", default=config.DITTO_URL)
    p.add_argument("--user", default="viewer")
    p.add_argument("--password", default=None, help="default: same as the user")
    p.add_argument("--no-history", action="store_true", help="do not query InfluxDB")
    p.add_argument("--log-file", default="logs/viz_server.log")
    args = p.parse_args()
    setup_logging(args.log_file)

    relay = TwinRelay(args.user, args.password, args.ditto_url)
    relay.ditto.wait_ready()
    relay.start()
    history = None
    if not args.no_history:
        from twin.history import History
        history = History()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(relay, history))
    httpd.daemon_threads = True
    log.info("[VIZ] 3D viewer on http://localhost:%d/3d/ (Ditto %s as '%s', history %s)", args.port,
             args.ditto_url, args.user, "off" if history is None else "InfluxDB")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        relay.ws.stop()


if __name__ == "__main__":
    main()
