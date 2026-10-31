"""Real-time dashboard: HTTP + Server-Sent Events (a remote subscriber of the twin state store)."""
import json
import logging
import queue
import threading
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import METRICS
from .model import ATTRIBUTES
from .state_store import QueueSubscriber, TwinStore

log = logging.getLogger("twin.web")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _json(obj) -> bytes:
    def default(o):
        return o.isoformat() if isinstance(o, datetime) else str(o)
    return json.dumps(obj, default=default, ensure_ascii=False).encode("utf-8")


def notification_json(n) -> dict:
    return {"printer_id": n.printer_id, "version": n.version, "reason": n.reason, "changes": n.changes,
            "events": n.events, "commit_ts": n.commit_ts, "trigger_ts": n.trigger_ts,
            "latency_ms": n.latency_ms, "snapshot": n.snapshot}


class DashboardServer:
    def __init__(self, store: TwinStore, port: int, history=None):
        self.store, self.port, self.history = store, port, history
        self.latest_metrics: dict = {}
        self._metric_listeners: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), self._handler())
        self.httpd.daemon_threads = True

    def on_metrics(self, row: dict):
        self.latest_metrics = row
        with self._lock:
            for q in self._metric_listeners:
                if q.qsize() < 10:
                    q.put_nowait(row)

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, name="dashboard", daemon=True).start()
        log.info("[WEB] Real-time dashboard: http://localhost:%d", self.port)

    def stop(self):
        self.httpd.shutdown()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(url.query).items()}
                try:
                    if url.path in ("/", "/index.html"):
                        self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                    elif url.path == "/api/state":
                        self._send(200, _json(server.store.snapshots()))
                    elif url.path == "/api/model":
                        self._send(200, _json({"attributes": [asdict(a) for a in ATTRIBUTES],
                                               "metrics": {k: asdict(v) for k, v in METRICS.items()}}))
                    elif url.path == "/api/metrics":
                        self._send(200, _json(server.latest_metrics))
                    elif url.path == "/api/stream":
                        self._stream()
                    elif url.path.startswith("/api/history/") and server.history is None:
                        self._send(503, _json({"error": "storage disabled"}))
                    elif url.path == "/api/history/series":
                        rows = server.history.series(qs["printer"], qs["metric"], qs.get("start", "-10m"),
                                                     qs.get("stop", "now()"), qs.get("source", "resampled"),
                                                     qs.get("every"))
                        self._send(200, _json(rows))
                    elif url.path == "/api/history/state_at":
                        self._send(200, _json(server.history.state_at(qs["printer"], qs["time"])))
                    elif url.path == "/api/history/events":
                        self._send(200, _json(server.history.events(qs.get("printer"), qs.get("start", "-1h"))))
                    elif url.path == "/api/history/fill_stats":
                        self._send(200, _json(server.history.fill_stats(qs["printer"], qs.get("start", "-1h"))))
                    else:
                        self._send(404, _json({"error": "not found"}))
                except (KeyError, ValueError) as exc:
                    self._send(400, _json({"error": str(exc)}))
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    log.exception("[WEB] request failed")
                    self._send(500, _json({"error": str(exc)}))

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                sub = QueueSubscriber(maxsize=2000)
                mq: queue.Queue = queue.Queue()
                unsubscribe = server.store.subscribe(sub)
                with server._lock:
                    server._metric_listeners.append(mq)
                try:
                    for pid, snap in server.store.snapshots().items():
                        self._event("snapshot", snap)
                    while True:
                        try:
                            n = sub.q.get(timeout=0.5)
                            self._event("commit", notification_json(n))
                            # send whatever accumulated in one go
                            while not sub.q.empty():
                                self._event("commit", notification_json(sub.q.get_nowait()))
                        except queue.Empty:
                            self.wfile.write(b": keep-alive\n\n")
                        while not mq.empty():
                            self._event("metrics", mq.get_nowait())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    unsubscribe()
                    with server._lock:
                        server._metric_listeners.remove(mq)

            def _event(self, name: str, data):
                self.wfile.write(f"event: {name}\ndata: ".encode() + _json(data) + b"\n\n")

        return Handler
