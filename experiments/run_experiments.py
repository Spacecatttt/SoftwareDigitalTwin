"""Runs the measurement scenarios (step 7-8 of the lab) and stores raw results in results/<run_id>/.

  python experiments/run_experiments.py                 # all scenarios (~25 min)
  python experiments/run_experiments.py normal outage   # selected ones

Every run starts a fresh pipeline process (clean MQTT session, storage on), starts the emulators,
waits until the broker queue is drained and stops the pipeline with SIGINT, so the pipeline writes its
summary (latency percentiles, counters) and the latency samples.
"""
import csv
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
PY = sys.executable

FAULTS = ["--p-invalid", "0.005", "--p-duplicate", "0.005", "--p-jitter", "0.05", "--jitter-ms", "300",
          "--p-drop", "0.002"]
SWEEP_LEVELS = [1, 10, 25, 30, 35, 40, 50, 75, 100, 150]   # intensity multipliers, 4 printers x 24 msg/s x level
REORDER_DELAYS_MS = [0, 50, 100, 200, 400]


def reset_live_session():
    """Discards the persistent session of the live pipeline, otherwise the broker would queue all
    QoS 1 test traffic for it and replay it at the next live start."""
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "DigitalTwin_Pipeline", clean_session=True)
    c.connect("localhost", 1883)
    c.loop_start()
    time.sleep(0.5)
    c.disconnect()
    c.loop_stop()


def wait_drained(metrics_csv: Path, timeout: float):
    """Waits until the pipeline received nothing for 3 consecutive seconds."""
    t_end = time.time() + timeout
    while time.time() < t_end:
        time.sleep(1.0)
        try:
            rows = list(csv.DictReader(open(metrics_csv)))
        except FileNotFoundError:
            continue
        tail = rows[-3:]
        if len(tail) == 3 and all(float(r["ingress_mps"]) == 0 and int(r["inbox_depth"]) == 0 for r in tail):
            return
    print("  ! drain timeout")


def run(run_id: str, emulators: dict[str, list[str]], pipeline_args=(), drain_timeout=180.0):
    out = RESULTS / run_id
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        f.unlink()
    print(f"[{time.strftime('%H:%M:%S')}] {run_id}: {len(emulators)} emulator(s) {' '.join(pipeline_args)}", flush=True)
    log = open(out / "pipeline.stdout", "w")
    pipe = subprocess.Popen([PY, "pipeline.py", "--run-id", run_id, "--clean-session", "--quiet", "--web-port", "0",
                             "--metrics-csv", str(out / "metrics.csv"), "--summary-json", str(out / "summary.json"),
                             "--latency-samples", str(out / "latency.csv"), "--log-file", str(out / "pipeline.log"),
                             *pipeline_args], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        time.sleep(0.1)
        if (out / "pipeline.log").exists() and "[MQTT] Connected" in (out / "pipeline.log").read_text():
            break
    time.sleep(1.0)
    t_start = time.time()
    emus = [subprocess.Popen([PY, "emulator.py", pid, "--quiet", "--stats-json", str(out / f"emulator_{pid}.json"), *args],
                             cwd=ROOT) for pid, args in emulators.items()]
    for e in emus:
        e.wait()
    t_emu_end = time.time()
    wait_drained(out / "metrics.csv", drain_timeout)
    pipe.send_signal(signal.SIGINT)
    pipe.wait(60)
    meta = {"run_id": run_id, "t_start": t_start, "t_emulators_end": t_emu_end, "t_end": time.time(),
            "emulators": emulators, "pipeline_args": list(pipeline_args)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    s = json.loads((out / "summary.json").read_text())
    e2e = s["e2e_ms"]
    print(f"    processed {s.get('processed', 0)}, e2e mean {e2e['mean'] or 0:.1f} ms, p95 {e2e['p95'] or 0:.1f}, "
          f"max {e2e['max'] or 0:.1f}", flush=True)


def scenario_normal():
    """S1: nominal operation, 2 printers at the lab 2 rates (10 Hz + 1 Hz), light data-quality faults."""
    run("S1_normal", {f"printer_0{i}": ["--duration", "120", "--seed", str(i), *FAULTS] for i in (1, 2)})


def scenario_burst():
    """S2: intensity bursts, 4 printers: x25 (within capacity) and x60 (beyond capacity) for 20 s each."""
    profile = "0:1,30:25,50:1,80:60,100:1"
    run("S2_burst", {f"printer_0{i}": ["--duration", "130", "--seed", str(i), "--profile", profile, "--no-spikes"]
                     for i in (1, 2, 3, 4)})


def scenario_outage():
    """S3: temporary loss of connectivity of printer_01 (20 s, abrupt disconnect => LWT, store-and-forward
    of QoS 1 data); printer_02 keeps working as a control."""
    run("S3_outage", {"printer_01": ["--duration", "100", "--seed", "1", "--outage", "40:20"],
                      "printer_02": ["--duration", "100", "--seed", "2"]})


def scenario_load_sweep():
    """Throughput: offered load sweep with and without writing to the time-series storage."""
    for storage in (True, False):
        for level in SWEEP_LEVELS:
            run(f"L_{'db' if storage else 'nodb'}_x{level}",
                {f"load_0{i}": ["--duration", "15", "--seed", str(i), "--rate", str(level), "--no-spikes"] for i in (1, 2, 3, 4)},
                [] if storage else ["--no-storage"], drain_timeout=240)


def scenario_reorder_sweep():
    """Reorder buffer: 30 % of messages delayed by 0..300 ms in the channel, different buffer delays."""
    for delay in REORDER_DELAYS_MS:
        run(f"R_{delay}ms", {f"printer_0{i}": ["--duration", "40", "--seed", str(i), "--p-jitter", "0.3",
                                                "--jitter-ms", "300", "--no-spikes"] for i in (1, 2)},
            ["--reorder-ms", str(delay)])


SCENARIOS = {"normal": scenario_normal, "burst": scenario_burst, "outage": scenario_outage,
             "load": scenario_load_sweep, "reorder": scenario_reorder_sweep}

if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    reset_live_session()
    for name in names:
        SCENARIOS[name]()
    print("done")
