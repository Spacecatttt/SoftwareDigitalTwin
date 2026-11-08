"""Performance of the 3D viewer (lab 5): frame rate at different numbers of printers and twin updates per second.

  python experiments/viz_bench.py run      # drives Chrome via the DevTools protocol, writes results/lab5/bench_gpu.json
  python experiments/viz_bench.py plot     # report/lab5/figures/fig_perf.png from results/lab5/*.json

The benchmark itself runs inside the page (web/3d/js/bench.js, URL parameter ?bench=1): synthetic twin changes go
through the same code path as the live ones. viz_server.py must be running. Chrome: CHROME=<path to chrome(.exe)>,
by default the Windows Chrome of the WSL host; the GPU is used through ANGLE (D3D11), --gpu=0 gives SwiftShader.
"""
import argparse
import base64
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results" / "lab5"
FIG = ROOT / "report" / "lab5" / "figures"
CHROME = os.getenv("CHROME", "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
PLANS = {
    "lod": None,                                     # default plan of bench.js
    "nolod": [{"printers": 3, "rate": 100}, {"printers": 12, "rate": 100}, {"printers": 48, "rate": 100},
              {"printers": 48, "rate": 1000}],
}


def chrome(port: int, gpu: bool):
    flags = ["--headless=new", f"--remote-debugging-port={port}", "--hide-scrollbars", "--no-first-run",
             f"--user-data-dir={os.getenv('CHROME_PROFILE', '/tmp')}/viz-bench-{port}"]
    flags += ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"] if gpu else ["--disable-gpu", "--enable-unsafe-swiftshader"]
    proc = subprocess.Popen([CHROME, *flags, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1)
            return proc
        except OSError:
            time.sleep(0.25)
    raise SystemExit("Chrome did not start")


def run_page(port: int, url: str, timeout_s: float = 300) -> dict:
    import websocket
    req = urllib.request.Request(f"http://localhost:{port}/json/new?about:blank", method="PUT")
    tab = json.load(urllib.request.urlopen(req))
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
    n = 0

    def call(method, **params):
        nonlocal n
        n += 1
        ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == n:
                return msg.get("result", {})

    call("Emulation.setDeviceMetricsOverride", width=1600, height=900, deviceScaleFactor=1, mobile=False)
    call("Page.navigate", url=url)
    deadline = time.time() + timeout_s
    result = None
    while time.time() < deadline:
        time.sleep(2)
        r = call("Runtime.evaluate", expression="window.__bench && window.__bench.done ? window.__bench : null",
                 returnByValue=True)
        result = r.get("result", {}).get("value")
        if result:
            break
    ws.close()
    urllib.request.urlopen(f"http://localhost:{port}/json/close/{tab['id']}")
    if not result:
        raise SystemExit(f"benchmark did not finish: {url}")
    return result


def cmd_run(args):
    proc = chrome(args.port, args.gpu)
    try:
        out = {}
        for name, plan in PLANS.items():
            url = f"{args.url}?bench=1" + ("&lod=0" if name == "nolod" else "")
            if plan:
                url += "&plan=" + json.dumps(plan, separators=(",", ":"))
            out[name] = run_page(args.port, url)
            for r in out[name]["results"]:
                print(f"{name:6} {r['printers']:3} printers {r['rate']:5}/s -> {r['fps']:6.1f} fps, "
                      f"p95 {r['frameP95']:5.1f} ms, CPU {r['cpuMean']:.2f} ms, {r['calls']} calls")
        RESULTS.mkdir(parents=True, exist_ok=True)
        name = "bench_gpu.json" if args.gpu else "bench_software.json"
        (RESULTS / name).write_text(json.dumps(out, indent=1), encoding="utf-8")
        print("->", RESULTS / name, "|", out["lod"]["gpu"])
    finally:
        proc.terminate()


def cmd_plot(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker
    g = json.loads((RESULTS / "bench_gpu.json").read_text(encoding="utf-8"))
    lod = [r for r in g["lod"]["results"]]
    nolod = g["nolod"]["results"]
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.6), dpi=150)

    def by_printers(rows, rate):
        pts = sorted({r["printers"]: r["fps"] for r in rows if r["rate"] == rate}.items())
        return [p for p, _ in pts], [f for _, f in pts]

    x, y = by_printers(lod, 100)
    a1.plot(x, y, "-o", color="#2a78d6", lw=2, ms=7, label="з LOD")
    a1.annotate(f"{y[-1]:.0f}", (x[-1], y[-1]), textcoords="offset points", xytext=(6, 4), color="#2a78d6")
    x2, y2 = by_printers(nolod, 100)
    a1.plot(x2, y2, "--s", color="#52514e", lw=2, ms=7, label="без LOD")
    a1.annotate(f"{y2[-1]:.0f}", (x2[-1], y2[-1]), textcoords="offset points", xytext=(6, -12), color="#52514e")
    a1.axhline(60, color="#898781", lw=1, ls=":")
    a1.text(3, 62, "60 кадр/с", color="#898781", fontsize=9)
    a1.set(xscale="log", xticks=[3, 12, 48], xticklabels=["3", "12", "48"], ylim=(0, 200),
           xlabel="принтерів у сцені (100 оновлень/с)", ylabel="кадрів за секунду")
    a1.legend(frameon=False, loc="lower left")
    a1.grid(axis="y", color="#eceef2")

    pts = sorted((r["rate"], r["fps"]) for r in lod if r["printers"] == 3)
    a2.plot([max(p, 1) for p, _ in pts], [f for _, f in pts], "-o", color="#2a78d6", lw=2, ms=7)
    a2.axhline(60, color="#898781", lw=1, ls=":")
    a2.set(xscale="log", ylim=(0, 200), xticks=[1, 10, 100, 1000, 5000],
           xticklabels=["0", "10", "100", "1000", "5000"], xlabel="оновлень двійників за секунду (3 принтери)",
           ylabel="кадрів за секунду")
    a2.grid(axis="y", color="#eceef2")
    for ax in (a1, a2):
        ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    fig.tight_layout()
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / "fig_perf.png")
    print("->", FIG / "fig_perf.png")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://localhost:8060/3d/")
    r.add_argument("--port", type=int, default=9340)
    r.add_argument("--gpu", type=int, default=1)
    sub.add_parser("plot")
    a = p.parse_args()
    {"run": cmd_run, "plot": cmd_plot}[a.cmd](a)


if __name__ == "__main__":
    main()
