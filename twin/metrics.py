"""Pipeline performance metrics: latency by stage, throughput, counters (per second and per run)."""
import math
import threading
from collections import defaultdict


def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def describe(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None, "min": None}
    s = sorted(vals)
    return {"n": len(s), "mean": sum(s) / len(s), "min": s[0], "p50": percentile(s, 50),
            "p95": percentile(s, 95), "p99": percentile(s, 99), "max": s[-1]}


class PipelineMetrics:
    """Latencies (ms) of every processed measurement:
       net  = t_recv  - t_gen    (device -> broker -> pipeline)
       proc = t_state - t_recv   (queueing + reorder buffer + preprocessing + state update)
       e2e  = t_state - t_gen    (generation -> digital twin working state updated)
       sync = t_commit - t_gen   (generation -> committed twin state, EVENT / THRESHOLD commits)
    """

    MAX_RUN_SAMPLES = 3_000_000

    def __init__(self):
        self._lock = threading.Lock()
        self.received = 0
        self.counters = defaultdict(int)
        self._win = self._new_window()
        self.run_e2e: list[float] = []
        self.run_net: list[float] = []
        self.run_proc: list[float] = []
        self.run_sync: dict[str, list[float]] = defaultdict(list)

    @staticmethod
    def _new_window():
        return {"e2e": [], "net": [], "proc": [], "sync": [], "counters": defaultdict(int)}

    def on_received(self):
        # called from the MQTT network thread
        with self._lock:
            self.received += 1
            self._win["counters"]["received"] += 1

    def count(self, name: str, n: int = 1):
        self.counters[name] += n
        self._win["counters"][name] += n

    def on_processed(self, t_gen: float, t_recv: float, t_state: float):
        e2e, net, proc = (t_state - t_gen) * 1e3, (t_recv - t_gen) * 1e3, (t_state - t_recv) * 1e3
        w = self._win
        w["e2e"].append(e2e)
        w["net"].append(net)
        w["proc"].append(proc)
        w["counters"]["processed"] += 1
        self.counters["processed"] += 1
        if len(self.run_e2e) < self.MAX_RUN_SAMPLES:
            self.run_e2e.append(e2e)
            self.run_net.append(net)
            self.run_proc.append(proc)

    def on_commit(self, reason: str, latency_ms: float | None):
        self.count(f"commits_{reason.lower()}")
        if latency_ms is not None:
            self._win["sync"].append(latency_ms)
            if len(self.run_sync[reason]) < self.MAX_RUN_SAMPLES:
                self.run_sync[reason].append(latency_ms)

    def roll(self, now: float, interval: float, gauges: dict) -> dict:
        """Closes the current per-second window and returns its statistics."""
        with self._lock:
            w, self._win = self._win, self._new_window()
        c = w["counters"]
        e2e, net, proc, sync = describe(w["e2e"]), describe(w["net"]), describe(w["proc"]), describe(w["sync"])
        row = {"ts": now, "received": c["received"], "processed": c["processed"],
               "throughput_mps": c["processed"] / interval, "ingress_mps": c["received"] / interval,
               "e2e_mean_ms": e2e["mean"], "e2e_p50_ms": e2e["p50"], "e2e_p95_ms": e2e["p95"],
               "e2e_p99_ms": e2e["p99"], "e2e_max_ms": e2e["max"],
               "net_mean_ms": net["mean"], "net_max_ms": net["max"], "proc_mean_ms": proc["mean"],
               "proc_max_ms": proc["max"], "sync_mean_ms": sync["mean"], "sync_max_ms": sync["max"]}
        for name in ("invalid", "duplicate", "late", "lost", "outlier", "resampled_points",
                     "resample_corrections", "commits_periodic", "commits_event", "commits_threshold",
                     "events", "status"):
            row[name] = c[name]
        row.update(gauges)
        return row

    def summary(self) -> dict:
        out = {"received": self.received, **dict(self.counters),
               "e2e_ms": describe(self.run_e2e), "net_ms": describe(self.run_net),
               "proc_ms": describe(self.run_proc)}
        for reason, vals in self.run_sync.items():
            out[f"sync_{reason.lower()}_ms"] = describe(vals)
        return out
