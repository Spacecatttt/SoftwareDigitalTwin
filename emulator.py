"""3D printer telemetry emulator

Same topics / payload / physics as in lab 2, plus test-scenario controls:
  --rate / --profile      data intensity multiplier (burst scenario), e.g. --profile 0:1,30:20,50:1
  --outage START:DUR      temporary loss of connectivity: abrupt disconnect (broker fires LWT),
                          QoS 1 metrics are buffered on the device (store-and-forward), QoS 0 are lost
  --runaway START:DUR     thermal runaway: nozzle temperature ramps +2 C/s, then cools down
                          (the command emergencyCooldown on factory/{id}/commands cuts the heater earlier)
  --p-invalid/--p-duplicate/--p-jitter/--p-drop   data quality faults for the preprocessing stage

Timestamps: the WSL2 wall clock is periodically stepped by time synchronisation (seconds back and forth),
so by default timestamps are UTC taken once at start + the monotonic clock (no steps; --wall-clock gives
the lab 2 behaviour). The diagnostic field t_mono (monotonic generation time) lets the pipeline on the
same host measure latency exactly; consumers that do not know it simply ignore it.

Usage:  python emulator.py printer_01 --duration 60
"""

import argparse
import heapq
import json
import math
import random
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# Base physical states (target values), as in lab 2
BASE_STATE = {
    "nozzle_temperature": 210.0,
    "bed_temperature": 60.0,
    "chamber_temperature": 40.0,
    "extruder_feed_rate": 50.0,  # mm/s
    "fan_speed_percent": 80.0,
    "stepper_current_x": 1.2,
}
SENSORS = {
    "extruder_feed_rate": ("feed_rate_01", "mm/s"),
    "stepper_current_x": ("current_x_01", "A"),
    "nozzle_temperature": ("nozzle_temp_01", "Celsius"),
    "bed_temperature": ("bed_temp_01", "Celsius"),
    "chamber_temperature": ("chamber_temp_01", "Celsius"),
    "fan_speed_percent": ("fan_speed_01", "%"),
}


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("printer_id", nargs="?", default="printer_01")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument(
        "--duration", type=float, default=0, help="seconds, 0 = until Ctrl+C"
    )
    p.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="intensity multiplier (1 = 10 Hz / 1 Hz as in lab 2)",
    )
    p.add_argument(
        "--profile",
        default="",
        help="time-varying multiplier: 't0:m0,t1:m1,...' (seconds:multiplier)",
    )
    p.add_argument(
        "--outage", default="", help="START:DURATION of a connectivity loss, seconds"
    )
    p.add_argument(
        "--outage-mode", choices=["disconnect", "silent"], default="disconnect"
    )
    p.add_argument(
        "--runaway", default="", help="START:DURATION of a thermal runaway, seconds"
    )
    p.add_argument(
        "--no-spikes", action="store_true", help="disable lab 2 anomaly spikes"
    )
    p.add_argument("--p-invalid", type=float, default=0.0)
    p.add_argument("--p-duplicate", type=float, default=0.0)
    p.add_argument("--p-jitter", type=float, default=0.0)
    p.add_argument("--jitter-ms", type=float, default=300.0)
    p.add_argument("--p-drop", type=float, default=0.0)
    p.add_argument(
        "--wall-clock",
        action="store_true",
        help="timestamps from the system wall clock (lab 2)",
    )
    p.add_argument("--seed", type=int)
    p.add_argument("--stats-json")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def parse_pair(s: str) -> tuple[float, float] | None:
    if not s:
        return None
    a, b = s.split(":")
    return float(a), float(b)


class Emulator:
    def __init__(self, args):
        self.args = args
        self.pid = args.printer_id
        self.rng = random.Random(args.seed)
        self.profile = (
            sorted(parse_pair(x) for x in args.profile.split(","))
            if args.profile
            else [(0.0, args.rate)]
        )
        self.outage = parse_pair(args.outage)
        self.runaway = parse_pair(args.runaway)
        self.t0 = time.monotonic()
        self.cooldown_t: float | None = None  # time of the emergencyCooldown command, s since start
        self.seq = Counter()
        self.stats = Counter()
        self.delayed: list = []  # jitter heap: (send_at, n, topic, payload, qos)
        self.edge_buffer: deque = deque(
            maxlen=100_000
        )  # store-and-forward during the outage
        self.link_up = True
        self.status_topic = f"factory/{self.pid}/status"
        self.clock_offset = time.time() - time.monotonic()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            f"Publisher_{self.pid}",
            clean_session=False,
        )
        self.client.max_inflight_messages_set(1000)
        self.client.max_queued_messages_set(0)
        # Last Will: if the publisher disappears, the broker publishes this message (lab 2)
        self.client.will_set(
            self.status_topic,
            json.dumps({"status": "OFFLINE", "reason": "unexpected_disconnect"}),
            qos=1,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_command

    def log(self, msg):
        if not self.args.quiet:
            print(f"[EMULATOR {self.pid}] {msg}", flush=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.log(f"Connected to {self.args.host}:{self.args.port}")
            client.publish(
                self.status_topic,
                json.dumps({"status": "ONLINE", "timestamp": self.now_iso()}),
                qos=1,
                retain=True,
            )
            client.subscribe(f"factory/{self.pid}/commands", qos=1)

    def _on_command(self, client, userdata, msg):
        """Commands from the digital twin platform (lab 4): forwarded by ditto_adapter.py from Ditto messages."""
        try:
            cmd = json.loads(msg.payload)
        except ValueError:
            return
        self.stats["commands"] += 1
        reason = (cmd.get("args") or {}).get("reason", "")
        if cmd.get("command") == "emergencyCooldown" and self.cooldown_t is None:
            self.cooldown_t = time.monotonic() - self.t0
            print(f"[EMULATOR {self.pid}] COMMAND emergencyCooldown ({reason}): heater power cut", flush=True)
        else:
            print(f"[EMULATOR {self.pid}] COMMAND {cmd.get('command')} ignored", flush=True)

    def now_iso(self, mono: float | None = None) -> str:
        if self.args.wall_clock:
            return datetime.now(timezone.utc).isoformat()
        ts = self.clock_offset + (time.monotonic() if mono is None else mono)
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    # --- intensity profile ------------------------------------------------------------------------
    def rate_at(self, t: float) -> float:
        rate = self.profile[0][1]
        for t0, m in self.profile:
            if t >= t0:
                rate = m
        return rate

    # --- publishing with fault injection ------------------------------------------------------------
    def publish_metric(self, metric: str, value: float, qos: int, now: float):
        topic = f"factory/{self.pid}/telemetry/{metric}"
        self.seq[metric] += 1
        sensor_id, unit = SENSORS[metric]
        mono = time.monotonic()
        data = {
            "sensor_id": sensor_id,
            "seq_id": self.seq[metric],
            "value": value,
            "unit": unit,
            "timestamp": self.now_iso(mono),
            "t_mono": round(mono, 6),
        }
        self.stats["generated"] += 1
        r = self.rng.random
        if not self.link_up:
            if qos > 0:
                self.edge_buffer.append((topic, json.dumps(data), qos))
                self.stats["buffered"] += 1
            else:
                self.stats["lost_in_outage"] += 1
            return
        if r() < self.args.p_drop:
            self.stats["dropped"] += 1
            return
        if r() < self.args.p_invalid:
            payload = self._corrupt(data)
            self.stats["invalid"] += 1
        else:
            payload = json.dumps(data)
        if r() < self.args.p_jitter:
            delay = r() * self.args.jitter_ms / 1000.0
            heapq.heappush(
                self.delayed, (now + delay, self.stats["jittered"], topic, payload, qos)
            )
            self.stats["jittered"] += 1
        else:
            self._send(topic, payload, qos)
        if r() < self.args.p_duplicate:
            self._send(topic, payload, qos)
            self.stats["duplicates"] += 1

    def _corrupt(self, data: dict) -> str:
        kind = self.rng.choice(["malformed", "missing", "nan", "range", "unit"])
        self.stats[f"invalid_{kind}"] += 1
        if kind == "malformed":
            return json.dumps(data)[:-7]
        if kind == "missing":
            return json.dumps({k: v for k, v in data.items() if k != "timestamp"})
        if kind == "nan":
            return json.dumps({**data, "value": "NaN"})
        if kind == "range":
            return json.dumps({**data, "value": -999.0})  # typical "sensor fault" code
        return json.dumps({**data, "unit": "F"})

    def _send(self, topic, payload, qos):
        self.client.publish(topic, payload, qos=qos)
        self.stats["sent"] += 1

    def flush_delayed(self, now: float):
        while self.delayed and self.delayed[0][0] <= now:
            _, _, topic, payload, qos = heapq.heappop(self.delayed)
            if self.link_up:
                self._send(topic, payload, qos)

    # --- connectivity loss --------------------------------------------------------------------------
    def link_down(self):
        self.link_up = False
        if self.args.outage_mode == "disconnect":
            # abrupt loss: no DISCONNECT packet, so the broker publishes the Last Will (OFFLINE)
            self.client.loop_stop()
            sock = self.client.socket()
            if sock:
                sock.close()
        self.log("LINK DOWN (QoS 1 data is buffered on the device, QoS 0 data is lost)")

    def link_restore(self):
        if self.args.outage_mode == "disconnect":
            self.client.reconnect()
            self.client.loop_start()
        self.link_up = True
        n = len(self.edge_buffer)
        while self.edge_buffer:
            topic, payload, qos = self.edge_buffer.popleft()
            self._send(topic, payload, qos)
            self.stats["flushed"] += 1
        self.log(f"LINK RESTORED, flushed {n} buffered messages")

    # --- physics (as in lab 2) ----------------------------------------------------------------------
    def step(self, step: int, t: float, now: float):
        g = self.rng.gauss
        # HIGH-FREQUENCY METRICS (10 Hz, QoS 0): feed rate by print phase, each phase lasts 5 s
        phase = (step // 50) % 4
        target_feed = [50.0, 120.0, 0.0, 60.0][phase]
        feed_rate = max(
            0.0, min(150.0, target_feed + g(0, 1.2) if target_feed > 0 else 0.0)
        )
        current_x = BASE_STATE["stepper_current_x"] + g(0, 0.02)
        if (
            not self.args.no_spikes and step % 300 == 0
        ):  # jamming / skipped steps spike every 30 s
            current_x += 0.5
        current_x = max(0.8, min(1.8, current_x))
        self.publish_metric("extruder_feed_rate", round(feed_rate, 2), 0, now)
        self.publish_metric("stepper_current_x", round(current_x, 3), 0, now)

        # LOW-FREQUENCY METRICS (1 Hz, QoS 1)
        if step % 10 == 0:
            wave = math.sin(step / 50.0)
            nozzle = (
                BASE_STATE["nozzle_temperature"]
                + wave * 2.0
                + g(0, 0.25)
                + self.runaway_offset(t)
            )
            if (
                not self.args.no_spikes and step % 50 == 0
            ):  # single-sample thermal spike (lab 2)
                nozzle += 35.0
            nozzle = max(180.0, min(320.0, nozzle))
            bed = max(
                30.0, min(110.0, BASE_STATE["bed_temperature"] + wave * 0.5 + g(0, 0.5))
            )
            chamber = max(
                30.0, min(70.0, BASE_STATE["chamber_temperature"] + g(0, 0.5))
            )
            fan = max(0.0, min(100.0, BASE_STATE["fan_speed_percent"] + g(0, 0.5)))
            self.publish_metric("nozzle_temperature", round(nozzle, 2), 1, now)
            self.publish_metric("bed_temperature", round(bed, 2), 1, now)
            self.publish_metric("chamber_temperature", round(chamber, 2), 1, now)
            self.publish_metric("fan_speed_percent", round(fan, 1), 1, now)

    def runaway_offset(self, t: float) -> float:
        if not self.runaway:
            return 0.0
        if self.cooldown_t is not None and t >= self.cooldown_t:
            # heater power cut: the overheating decays with a 3 s time constant, then regulation resumes
            return self._runaway_ramp(self.cooldown_t) * math.exp(-(t - self.cooldown_t) / 3.0)
        return self._runaway_ramp(t)

    def _runaway_ramp(self, t: float) -> float:
        start, dur = self.runaway
        if t < start:
            return 0.0
        if t < start + dur:
            return 2.0 * (t - start)
        return max(0.0, 2.0 * dur - 2.0 * (t - start - dur))

    # --- main loop: deadline scheduling keeps the average rate exact even at high multipliers --------
    def run(self):
        self.client.connect(self.args.host, self.args.port, keepalive=10)
        self.client.loop_start()
        t0 = self.t0 = time.monotonic()
        next_t, step = t0, 0
        outage_started = outage_done = False
        try:
            while True:
                now = time.monotonic()
                t = now - t0
                if self.args.duration and t >= self.args.duration:
                    break
                if self.outage and not outage_started and t >= self.outage[0]:
                    outage_started = True
                    self.link_down()
                if (
                    self.outage
                    and outage_started
                    and not outage_done
                    and t >= sum(self.outage)
                ):
                    outage_done = True
                    self.link_restore()
                self.flush_delayed(now)
                if now >= next_t:
                    step += 1
                    self.step(step, t, now)
                    next_t += 0.1 / self.rate_at(t)
                    if (
                        now - next_t > 1.0
                    ):  # cannot keep up: do not accumulate unbounded lag
                        self.stats["lagged_resets"] += 1
                        next_t = now
                else:
                    time.sleep(min(next_t - now, 0.005))
        except KeyboardInterrupt:
            self.log("Stopped by user")
        finally:
            self.flush_delayed(float("inf"))
            self.stats["steps"] = step
            self.stats["elapsed_s"] = round(time.monotonic() - t0, 3)
            # graceful shutdown (lab 2)
            info = self.client.publish(
                self.status_topic,
                json.dumps(
                    {
                        "status": "OFFLINE",
                        "reason": "normal_shutdown",
                        "timestamp": self.now_iso(),
                    }
                ),
                qos=1,
                retain=True,
            )
            try:
                info.wait_for_publish(timeout=3)
            except (RuntimeError, ValueError):
                pass
            time.sleep(0.3)
            self.client.disconnect()
            self.client.loop_stop()
            self.log(f"Work completed: {dict(self.stats)}")
            if self.args.stats_json:
                with open(self.args.stats_json, "w") as f:
                    json.dump({"printer_id": self.pid, **self.stats}, f, indent=2)


if __name__ == "__main__":
    Emulator(parse_args()).run()
