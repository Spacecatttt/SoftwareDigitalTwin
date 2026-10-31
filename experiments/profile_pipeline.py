"""Samples which thread of the pipeline holds the GIL under load (py-spy --gil):
   python experiments/profile_pipeline.py [multiplier]   ->  results/profile/
"""
import collections
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "profile"
PY = sys.executable
PYSPY = str(Path(PY).with_name("py-spy"))


def main(level: int):
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / f"pyspy_x{level}.txt"
    prof = subprocess.Popen([PYSPY, "record", "--gil", "--threads", "--rate", "250", "--format", "raw", "-o", str(raw),
                             "--", PY, "pipeline.py", "--run-id", f"profile_x{level}", "--clean-session", "--quiet",
                             "--web-port", "0", "--duration", "22", "--log-file", str(OUT / "pipeline.log"),
                             "--metrics-csv", str(OUT / f"metrics_x{level}.csv")], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(3)
    emus = [subprocess.Popen([PY, "emulator.py", f"load_0{i}", "--quiet", "--duration", "15", "--rate", str(level),
                              "--no-spikes"], cwd=ROOT) for i in (1, 2, 3, 4)]
    for e in emus:
        e.wait()
    prof.wait()
    summarize(raw)


def classify(stack: str) -> str:
    if "paho" in stack:
        return "MQTT network thread (paho: socket read, MQTT packet parsing, on_message)"
    if "influx" in stack or "urllib3" in stack:
        return "InfluxDB writer thread (HTTP batches)"
    if "MqttRepublisher" in stack or "republisher" in stack:
        return "MQTT republisher of twin state"
    return "processing thread (pipeline stages)"


def summarize(raw: Path):
    threads, funcs = collections.Counter(), collections.defaultdict(collections.Counter)
    for ln in raw.read_text().splitlines():
        stack, _, n = ln.rpartition(" ")
        if not n.isdigit():
            continue
        n = int(n)
        frames = stack.split(";")
        group = classify(stack)
        threads[group] += n
        leaf_own = next((f for f in reversed(frames) if "digitaltwinSoftware" in f and "venv/" not in f), frames[-1])
        funcs[group][leaf_own.split(" (")[0] + " (" + leaf_own.split("/")[-1]] += n
    total = sum(threads.values())
    lines = [f"GIL samples: {total}"]
    for g, n in threads.most_common():
        lines.append(f"{100 * n / total:5.1f} %  {g}")
        for f, k in funcs[g].most_common(6):
            lines.append(f"          {100 * k / total:5.1f} %  {f}")
    text = "\n".join(lines)
    (raw.with_suffix(".summary.txt")).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    level = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    main(level)
    for lab, pct in stages(OUT / f"pyspy_x{level}.txt"):
        print(f"{pct:5.1f} %  {lab}")


STAGES = [  # first marker found in the stack (from the root) decides the stage of the processing thread
    ("commit (twin/state_store.py", "синхронізація: commit, знімок стану, сповіщення підписників, запис twin_state"),
    ("tick (twin/processor.py", "таймери: закриття інтервалів, watchdog, періодичне правило, метрики"),
    ("parse_message", "розбір JSON і валідація"),
    ("is_duplicate", "дедуплікація"),
    ("push (twin/ingest.py", "буфер упорядкування"),
    ("pop_ready", "буфер упорядкування"),
    ("next_release", "буфер упорядкування"),
    ("observe (twin/ingest.py", "виявлення втрат за seq_id"),
    ("write (twin/storage.py", "передавання рядків записувачу InfluxDB (черга)"),
    ("update (twin/preprocessing.py", "фільтр Гампеля"),
    ("apply_measurement", "оновлення робочого стану та похідних показників"),
    ("record_", "лічильники якості / аномалій"),
    ("line (twin/storage.py", "кодування line protocol (telemetry_raw, telemetry_resampled)"),
    ("add (twin/preprocessing.py", "ресемплінг"),
    ("on_processed", "метрики затримки"),
    ("get (queue.py", "черга inbox"),
    ("get_nowait", "черга inbox"),
]


def stages(raw: Path) -> list[tuple[str, float]]:
    cnt = collections.Counter()
    for ln in raw.read_text().splitlines():
        stack, _, n = ln.rpartition(" ")
        if not n.isdigit() or classify(stack) != "processing thread (pipeline stages)":
            continue
        frames = stack.split(";")
        label = "інше (цикл обробки, журнал)"
        for f in frames:
            hit = next((lab for marker, lab in STAGES if f.startswith(marker) or marker in f), None)
            if hit:
                label = hit
                break
        cnt[label] += int(n)
    total = sum(cnt.values())
    return [(lab, 100.0 * n / total) for lab, n in cnt.most_common()]
