"""Thin client of the Eclipse Ditto API: HTTP (things, policies, search, messages, WoT) and WebSocket (Ditto Protocol)."""
import base64
import json
import logging
import threading
import time
import urllib.parse

import requests
import websocket

from .config import DITTO_URL

log = logging.getLogger("twin.ditto")


class DittoError(Exception):
    def __init__(self, status: int, body: str, method: str = "", path: str = ""):
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.status, self.body = status, body

    @property
    def json(self) -> dict:
        try:
            return json.loads(self.body)
        except ValueError:
            return {}


class DittoClient:
    def __init__(self, user: str = "ditto", password: str | None = None, url: str = DITTO_URL, timeout: float = 10.0):
        self.url, self.user, self.timeout = url.rstrip("/"), user, timeout
        self.session = requests.Session()
        self.session.auth = (user, password if password is not None else user)

    # --- low level ----------------------------------------------------------------------------------
    def request(self, method: str, path: str, *, body=None, params=None, headers=None, ok=(200, 201, 202, 204),
                content_type="application/json") -> requests.Response:
        h = dict(headers or {})
        data = None
        if body is not None:
            h["Content-Type"] = content_type
            data = json.dumps(body)
        r = self.session.request(method, f"{self.url}/api/2{path}", data=data, params=params, headers=h,
                                 timeout=self.timeout)
        if r.status_code not in ok:
            raise DittoError(r.status_code, r.text, method, path)
        return r

    @staticmethod
    def _json(r: requests.Response):
        return r.json() if r.content else None

    # --- things -------------------------------------------------------------------------------------
    def get_thing(self, thing_id: str, fields: str | None = None, headers: dict | None = None) -> dict | None:
        try:
            return self._json(self.request("GET", f"/things/{thing_id}",
                                           params={"fields": fields} if fields else None, headers=headers))
        except DittoError as e:
            if e.status == 404:
                return None
            raise

    def put_thing(self, thing_id: str, thing: dict) -> int:
        return self.request("PUT", f"/things/{thing_id}", body=thing).status_code

    def merge_thing(self, thing_id: str, patch: dict, path: str = "", correlation_id: str | None = None,
                    if_match: str | None = None) -> int:
        """RFC 7396 JSON merge patch: only the given fields change (null deletes a field). Without a condition a
        merge on a missing Thing creates it; if_match="*" makes it update-only (412 if the Thing does not exist)."""
        headers = {}
        if correlation_id:
            headers["correlation-id"] = correlation_id
        if if_match:
            headers["If-Match"] = if_match
        return self.request("PATCH", f"/things/{thing_id}{path}", body=patch, headers=headers,
                            content_type="application/merge-patch+json").status_code

    def delete_thing(self, thing_id: str) -> bool:
        try:
            self.request("DELETE", f"/things/{thing_id}")
            return True
        except DittoError as e:
            if e.status == 404:
                return False
            raise

    def thing_description(self, thing_id: str, feature: str | None = None) -> dict:
        path = f"/things/{thing_id}" + (f"/features/{feature}" if feature else "")
        return self._json(self.request("GET", path, headers={"Accept": "application/td+json"}))

    # --- policies -----------------------------------------------------------------------------------
    def put_policy(self, policy_id: str, policy: dict) -> int:
        return self.request("PUT", f"/policies/{policy_id}", body=policy).status_code

    # --- search -------------------------------------------------------------------------------------
    def search(self, rql: str | None = None, fields: str | None = None, sort: str = "+thingId",
               namespaces: str | None = None, page_size: int = 200, limit: int = 10_000) -> list[dict]:
        """Things matching an RQL filter; follows the search cursor over all pages."""
        out, cursor = [], None
        while len(out) < limit:
            options = [f"size({min(page_size, limit - len(out))})"] + ([f"cursor({cursor})"] if cursor else [f"sort({sort})"])
            params = {"option": ",".join(options)}
            if rql:
                params["filter"] = rql
            if fields:
                params["fields"] = fields
            if namespaces:
                params["namespaces"] = namespaces
            page = self._json(self.request("GET", "/search/things", params=params))
            out += page.get("items", [])
            cursor = page.get("cursor")
            if not cursor:
                break
        return out

    def count(self, rql: str | None = None) -> int:
        return int(self.request("GET", "/search/things/count", params={"filter": rql} if rql else None).text)

    # --- messages -----------------------------------------------------------------------------------
    def send_feature_message(self, thing_id: str, feature: str, subject: str, payload: dict,
                             timeout_s: int = 0, correlation_id: str | None = None) -> int:
        """Message to the feature inbox (WoT action); timeout 0 = fire and forget (202 Accepted)."""
        headers = {"correlation-id": correlation_id} if correlation_id else None
        return self.request("POST", f"/things/{thing_id}/features/{feature}/inbox/messages/{subject}",
                            body=payload, params={"timeout": str(timeout_s)}, headers=headers).status_code

    def wait_ready(self, timeout_s: float = 300.0):
        """Ditto services join their cluster for ~1 min after start; wait until the search API answers."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self.count()
                return
            except (requests.RequestException, DittoError) as e:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Ditto is not ready at {self.url}: {e}")
                time.sleep(2.0)


def rql_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class DittoWebSocket(threading.Thread):
    """Ditto Protocol over WebSocket with automatic reconnect. `subscriptions` are the control messages
    sent after (re)connecting, e.g. START-SEND-EVENTS?filter=...; on_message gets every Ditto Protocol message."""

    def __init__(self, user: str, password: str | None, subscriptions: list[str], on_message, url: str = DITTO_URL,
                 name: str = "ditto-ws"):
        super().__init__(name=name, daemon=True)
        ws_url = url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/ws/2"
        self.ws_url, self.auth = ws_url, (user, password if password is not None else user)
        self.subscriptions, self.on_message = subscriptions, on_message
        self.connected = threading.Event()
        self._stop_event = threading.Event()
        self._ws: websocket.WebSocket | None = None

    def run(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                ws = websocket.create_connection(self.ws_url, timeout=30, header=[_basic_auth(*self.auth)])
                self._ws = ws
                for sub in self.subscriptions:
                    ws.send(sub)
                    while True:      # events of an earlier subscription may arrive before this ACK
                        raw = ws.recv()
                        if raw.endswith(":ACK"):
                            break
                        self.on_message(json.loads(raw))
                    log.info("[WS] %s -> %s", sub.split("?")[0], raw)
                self.connected.set()
                backoff = 1.0
                ws.settimeout(None)
                while not self._stop_event.is_set():
                    raw = ws.recv()
                    if not raw or raw.endswith(":ACK"):
                        continue
                    try:
                        self.on_message(json.loads(raw))
                    except Exception:  # a faulty handler must not drop the subscription
                        log.exception("[WS] handler failed")
            except Exception as e:
                self.connected.clear()
                if self._stop_event.is_set():
                    break
                log.warning("[WS] %s, reconnecting in %.0f s", e.__class__.__name__, backoff)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def send(self, msg: dict):
        if self._ws is not None:
            self._ws.send(json.dumps(msg))

    def stop(self):
        self._stop_event.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass


def _basic_auth(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Authorization: Basic {token}"


def subscription(command: str, **params) -> str:
    """START-SEND-EVENTS?namespaces=...&filter=... with URL-encoded parameter values."""
    q = "&".join(f"{k}={urllib.parse.quote(v, safe=',/')}" for k, v in params.items() if v)
    return f"{command}?{q}" if q else command
