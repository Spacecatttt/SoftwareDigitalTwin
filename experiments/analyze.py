"""Builds tables and figures for the report from results/<run>/ and from the time-series storage.

  python experiments/analyze.py      ->  results/tables.md, results/analysis.json, report/figures/*.png
"""
import csv
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from twin.config import METRICS, SYNC  # noqa: E402
from twin.history import History  # noqa: E402

RESULTS = ROOT / "results"
FIG = ROOT / "report" / "figures"

# reference palette (light mode): categorical slots 1-3, ordinal blue ramp, ink and chrome
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"
RAMP = {"mean": "#86b6ef", "p95": "#2a78d6", "max": "#104281"}
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
SHADE = "#f0efec"
CRITICAL, WARNING, GOOD = "#d03b3b", "#fab219", "#0ca30c"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans", "font.size": 9.5, "axes.titlesize": 10.5, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False, "legend.fontsize": 8.5,
    "lines.linewidth": 1.6, "text.color": INK,
})


# ---------------------------------------------------------------------------------------------------
def load_run(run_id: str) -> dict | None:
    d = RESULTS / run_id
    if not (d / "summary.json").exists():
        return None
    rows = [{k: _num(v) for k, v in r.items()} for r in csv.DictReader(open(d / "metrics.csv"))]
    active = [i for i, r in enumerate(rows) if r["ingress_mps"] or r["processed"]]
    t0 = rows[active[0]]["ts"] if active else rows[0]["ts"]
    for r in rows:
        r["t"] = r["ts"] - t0
    emus = {p.stem.replace("emulator_", ""): json.loads(p.read_text()) for p in d.glob("emulator_*.json")}
    lat = None
    if (d / "latency.csv").exists():
        lat = [float(r["e2e_ms"]) for r in csv.DictReader(open(d / "latency.csv"))]
    return {"id": run_id, "rows": rows, "summary": json.loads((d / "summary.json").read_text()),
            "meta": json.loads((d / "meta.json").read_text()), "emulators": emus, "e2e": lat}


def _num(v):
    if v in ("", None):
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except ValueError:
        return v


def window(run, pad_s=2.0):
    """Wall-clock window of the run for storage queries (RFC3339)."""
    rows = run["rows"]
    a, b = rows[0]["ts"] - pad_s, rows[-1]["ts"]
    f = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    return f(a), f(b)


def save(fig, name):
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / name, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("  figure", name)


def shade(ax, a, b, label=None):
    ax.axvspan(a, b, color=SHADE, zorder=0, lw=0)
    if label:
        ax.text((a + b) / 2, 0.97, label, transform=ax.get_xaxis_transform(), ha="center", va="top",
                fontsize=8.5, color=INK2)


def offered_mps(run) -> float:
    e = run["emulators"].values()
    return sum(s.get("sent", 0) for s in e) / max(max(s.get("elapsed_s", 1) for s in e), 1e-9)


def fmt(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


# ---------------------------------------------------------------------------------------------------
def fig_timeline(run, name, title, phases=(), log_latency=False):
    rows = [r for r in run["rows"] if r["t"] >= -1]
    t = [r["t"] for r in rows]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 5.4), sharex=True, gridspec_kw={"hspace": 0.28})
    for p in phases:
        shade(a1, p[0], p[1], p[2])
        shade(a2, p[0], p[1])
    a1.plot(t, [r["ingress_mps"] for r in rows], color=C2, lw=1.4, label="надійшло")
    a1.plot(t, [r["throughput_mps"] for r in rows], color=C1, lw=1.6, label="оброблено")
    a1.set_ylabel("повідомлень / с")
    a1.set_ylim(0, 1.3 * max(max(r["ingress_mps"] for r in rows), max(r["throughput_mps"] for r in rows)))
    a1.set_title(title + ": інтенсивність потоку")
    a1.legend(loc="center left" if phases else "upper left", ncol=2 if not phases else 1)
    for key, lab in (("max", "максимум"), ("p95", "p95"), ("mean", "середнє")):
        vals = [r[f"e2e_{key}_ms"] for r in rows]
        a2.plot(t, vals, color=RAMP[key], lw=1.5 if key != "max" else 1.1, label=lab)
    a2.set_ylabel("мс")
    if log_latency:
        a2.set_yscale("log")
    a2.set_title("Затримка «генерація вимірювання → оновлення стану двійника» (за кожну секунду)")
    a2.set_xlabel("час від початку сценарію, с")
    a2.legend(loc="upper left", ncol=3)
    save(fig, name)


def fig_latency_hist(run, name):
    vals = sorted(run["e2e"])
    s = run["summary"]["e2e_ms"]
    fig, ax = plt.subplots(figsize=(9, 3.2))
    lo, hi = vals[0], s["p99"] * 1.15
    bins = [lo + (hi - lo) * i / 60 for i in range(61)]
    ax.hist([v for v in vals if v <= hi], bins=bins, color=C1, edgecolor=SURFACE, linewidth=0.6)
    for key, lab in (("mean", "середнє"), ("p95", "p95"), ("p99", "p99")):
        ax.axvline(s[key], color=INK2, lw=0.8)
        ax.text(s[key], 0.97, f" {lab} {s[key]:.1f}", transform=ax.get_xaxis_transform(), va="top",
                fontsize=8, color=INK2)
    ax.set_xlabel("затримка генерація → стан двійника, мс")
    ax.set_ylabel("кількість вимірювань")
    ax.set_title(f"Розподіл затримки, штатний режим (n = {s['n']}, максимум {s['max']:.1f} мс поза графіком)")
    save(fig, name)


def connectivity_from_log(log: Path, printer: str) -> list[dict]:
    """Connectivity changes committed by this pipeline run (events in the storage cannot be attributed to a
    run by time alone: processes started at different moments have different WSL wall-clock offsets)."""
    pat = re.compile(r"^(\S+ \S+) \[\w+\] \[TWIN " + re.escape(printer) + r" v\d+ EVENT\] connectivity: (\w+) -> (\w+)")
    out = []
    for ln in log.read_text(encoding="utf-8").splitlines():
        m = pat.match(ln)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").timestamp()
            out.append({"ts": ts, "old": m.group(2), "new": m.group(3)})
    return out


def fig_s3(run, hist: History):
    """Outage: connectivity of printer_01, ingress, latency, and the resampled series around the gap."""
    a, b = window(run)
    rows = run["rows"]
    t0 = next(r["ts"] for r in rows if r["ingress_mps"])
    rel = lambda dt: dt.timestamp() - t0
    events = connectivity_from_log(RESULTS / run["id"] / "pipeline.log", "printer_01")
    nozzle = hist.series("printer_01", "nozzle_temperature", a, b)
    feed = hist.series("printer_01", "extruder_feed_rate", a, b)

    fig, axes = plt.subplots(4, 1, figsize=(9, 9.4), sharex=True,
                             gridspec_kw={"hspace": 0.35, "height_ratios": [0.55, 1.1, 1, 1.2]})
    a0, a1, a2, a3 = axes
    # connectivity band
    segs, cur, start = [], "ONLINE", 0.0
    for e in events:
        tt = e["ts"] - t0
        if tt < 0:
            cur = e["new"]
            continue
        segs.append((start, tt, cur))
        cur, start = e["new"], tt
    segs.append((start, rows[-1]["ts"] - t0, cur))
    colors = {"ONLINE": GOOD, "OFFLINE": CRITICAL, "STALE": WARNING, "UNKNOWN": MUTED}
    outage = [s for s in segs if s[2] != "ONLINE"]
    for s0, s1, st in segs:
        if s1 > s0:
            a0.barh(0, s1 - s0, left=s0, height=0.6, color=colors.get(st, MUTED), edgecolor=SURFACE, lw=1.5)
            if s1 - s0 > 6:
                a0.text((s0 + s1) / 2, 0, st, ha="center", va="center", fontsize=8.5,
                        color="#ffffff" if st != "STALE" else INK)
    a0.set_yticks([])
    a0.grid(False)
    a0.set_title("printer_01: стан зв'язку в синхронізованому стані двійника (LWT → OFFLINE, відновлення → ONLINE)")
    for ax in axes[1:]:
        for s0, s1, st in outage:
            shade(ax, s0, s1)
    t = [r["t"] for r in rows]
    a1.plot(t, [r["ingress_mps"] for r in rows], color=C2, lw=1.3, label="надійшло")
    a1.plot(t, [r["throughput_mps"] for r in rows], color=C1, lw=1.5, label="оброблено")
    a1.set_ylabel("повід. / с")
    a1.set_title("Інтенсивність потоку (обидва принтери); сірим — обрив зв'язку printer_01")
    a1.legend(loc="upper left", ncol=2)
    a2.plot(t, [r["e2e_max_ms"] for r in rows], color=RAMP["max"], lw=1.1, label="максимум")
    a2.plot(t, [r["e2e_mean_ms"] for r in rows], color=RAMP["mean"], lw=1.5, label="середнє")
    a2.set_yscale("log")
    a2.set_ylabel("мс")
    a2.set_title("Затримка генерація → стан: буфер store-and-forward доставлено із запізненням")
    a2.legend(loc="upper left", ncol=2)
    # resampled nozzle temperature by fill method
    pts = [(rel(r["_time"]), r.get("mean"), r.get("fill")) for r in nozzle]
    meas = [(x, v) for x, v, f in pts if f == "measured"]
    a3.plot([x for x, _ in meas], [v for _, v in meas], color=C1, lw=1.3, label="температура сопла (виміряно)")
    for fill, col, mk, lab in (("locf", C2, "o", "LOCF"), ("interpolated", C3, "D", "інтерпольовано")):
        sel = [(x, v) for x, v, f in pts if f == fill and v is not None]
        if sel:
            a3.scatter([x for x, _ in sel], [v for _, v in sel], s=22, color=col, marker=mk, zorder=3,
                       edgecolor=SURFACE, linewidth=1, label=f"{lab} ({len(sel)})")
    fpts = [(rel(r["_time"]), r.get("fill")) for r in feed]
    gaps = [x for x, f in fpts if f in ("missing",)]
    for x in gaps:
        a3.axvspan(x, x + 1, ymin=0, ymax=0.06, color=CRITICAL, lw=0)
    a3.set_ylabel("°C")
    a3.set_title("Сітка 1 с: температура сопла (QoS 1, дозаповнена з буфера) і пропуски подачі (QoS 0, червоні мітки)")
    lo, hi = a3.get_ylim()
    a3.set_ylim(lo, hi + 0.35 * (hi - lo))
    a3.legend(loc="upper center", ncol=3)
    a3.set_xlabel("час від початку сценарію, с")
    save(fig, "fig_s3_outage.png")
    fills_feed = {}
    for _, f in fpts:
        fills_feed[f] = fills_feed.get(f, 0) + 1
    fills_nozzle = {}
    for _, _, f in pts:
        fills_nozzle[f] = fills_nozzle.get(f, 0) + 1
    return {"connectivity_segments": [(round(s0, 1), round(s1, 1), st) for s0, s1, st in segs],
            "fills_nozzle": fills_nozzle, "fills_feed": fills_feed}


def deadband_tradeoff(run, hist: History):
    """Replays the filtered series of S1 against the threshold rule with scaled deadbands
    (+ the 1 s periodic rule) and measures update rate vs. deviation of the twin from the measured value."""
    a, b = window(run)
    out = {}
    for metric in ("nozzle_temperature", "extruder_feed_rate", "stepper_current_x"):
        rows = hist.series("printer_01", metric, a, b, source="raw")
        series = [(r["_time"].timestamp(), r["filtered"]) for r in rows if r.get("filtered") is not None]
        if len(series) < 10:
            continue
        dur = series[-1][0] - series[0][0]
        base = METRICS[metric].deadband
        res = []
        for mult in (0, 0.5, 1, 2, 4, 8):
            db = base * mult
            committed, last_periodic, n_thr, errs = None, series[0][0], 0, []
            for ts, v in series:
                if ts - last_periodic >= SYNC.periodic_interval_s:
                    committed, last_periodic = v, ts
                if committed is None or abs(v - committed) >= db - 1e-12:
                    if committed is not None:
                        n_thr += 1
                    committed = v
                errs.append(abs(v - committed))
            res.append({"mult": mult, "deadband": db, "updates_per_s": n_thr / dur,
                        "mean_err": sum(errs) / len(errs), "max_err": max(errs)})
        out[metric] = {"samples": len(series), "rate_hz": len(series) / dur, "unit": METRICS[metric].unit, "points": res}
    if not out:
        return out
    fig, axes = plt.subplots(2, len(out), figsize=(9.5, 5.2), gridspec_kw={"hspace": 0.45, "wspace": 0.35})
    names = {"nozzle_temperature": "Температура сопла", "extruder_feed_rate": "Подача екструдера",
             "stepper_current_x": "Струм двигуна X"}
    units = {"Celsius": "°C", "mm/s": "мм/с", "A": "А"}
    for j, (metric, d) in enumerate(out.items()):
        pts = d["points"]
        x = [p["deadband"] for p in pts]
        ax = axes[0][j]
        ax.plot(x, [p["updates_per_s"] for p in pts], color=C1, marker="o", ms=4)
        ax.axhline(d["rate_hz"], color=MUTED, lw=0.8)
        ax.text(x[-1], d["rate_hz"], "частота вимірювань", ha="right", va="bottom", fontsize=7.5, color=INK2)
        ax.set_title(names[metric], fontsize=9.5)
        ax.set_ylabel("оновлень THRESHOLD / с" if j == 0 else "")
        ax.axvline(METRICS[metric].deadband, color=C2, lw=0.9)
        ax = axes[1][j]
        ax.plot(x, [p["mean_err"] for p in pts], color=RAMP["p95"], marker="o", ms=4, label="середнє")
        ax.plot(x, [p["max_err"] for p in pts], color=RAMP["max"], marker="s", ms=3.5, lw=1.1, label="максимум")
        ax.axvline(METRICS[metric].deadband, color=C2, lw=0.9)
        ax.set_xlabel(f"поріг зміни, {units[d['unit']]}")
        ax.set_ylabel("відхилення стану" if j == 0 else "")
        if j == 0:
            ax.legend(loc="upper left")
    fig.suptitle("Правило THRESHOLD: частота оновлень і відхилення синхронізованого стану від виміряного значення\n"
                 "(відтворення потоку S1 з періодичним правилом 1 с; помаранчева лінія — обраний поріг)",
                 x=0.01, ha="left", fontsize=10, fontweight="bold")
    save(fig, "fig_deadband.png")
    return out


def fig_sync_counts(s1):
    s = s1["summary"]
    cc = s["commit_counts"]
    processed = s["processed"]
    labels = ["Вимірювань оброблено", "THRESHOLD (поріг зміни)", "EVENT (подія)", "PERIODIC (період 1 с)"]
    vals = [processed, cc["THRESHOLD"], cc["EVENT"], cc["PERIODIC"]]
    fig, ax = plt.subplots(figsize=(9, 2.6))
    y = list(range(len(vals)))[::-1]
    ax.barh(y, vals, color=[MUTED, C1, C2, C3], height=0.62, edgecolor=SURFACE, linewidth=2)
    for yi, v in zip(y, vals):
        ax.text(v, yi, f" {v}  ({100 * v / processed:.1f} %)" if v != processed else f" {v}", va="center",
                fontsize=8.5, color=INK2)
    ax.set_yticks(y, labels)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, processed * 1.18)
    ax.set_title("Штатний режим: кількість оновлень синхронізованого стану за правилами (2 принтери, 120 с)")
    save(fig, "fig_sync_counts.png")


def fig_load(levels):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.9), gridspec_kw={"wspace": 0.28})
    for key, col, lab in (("db", C1, "із записом у InfluxDB"), ("nodb", C2, "без запису у сховище")):
        pts = sorted(levels[key], key=lambda p: p["offered"])
        if not pts:
            continue
        x = [p["offered"] for p in pts]
        a1.plot(x, [p["throughput"] for p in pts], color=col, marker="o", ms=4, label=lab)
        a2.plot(x, [p["p95"] for p in pts], color=col, marker="o", ms=4, label=f"p95, {lab}")
        a2.plot(x, [p["p50"] for p in pts], color=col, marker="o", ms=3, lw=1, alpha=0.55, label=f"медіана, {lab}")
    lim = max(p["offered"] for v in levels.values() for p in v) * 1.05
    a1.plot([0, lim], [0, lim], color=AXIS, lw=0.9, zorder=1)
    a1.text(lim * 0.97, lim * 0.97, "оброблено = надійшло", ha="right", va="top", fontsize=7.5, color=MUTED, rotation=0)
    a1.set_xlabel("запропоноване навантаження, повід./с")
    a1.set_ylabel("оброблено, повід./с")
    a1.set_title("Пропускна здатність конвеєра")
    a1.legend(loc="upper left")
    a2.set_yscale("log")
    a2.set_xlabel("запропоноване навантаження, повід./с")
    a2.set_ylabel("затримка, мс")
    a2.set_title("Затримка генерація → стан")
    a2.legend(loc="upper left", fontsize=7.5)
    save(fig, "fig_load.png")


def fig_reorder(rows):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.4), gridspec_kw={"wspace": 0.28})
    x = [r["delay"] for r in rows]
    a1.plot(x, [r["late_pct"] for r in rows], color=C2, marker="o", ms=4)
    a1.set_xlabel("затримка буфера впорядкування, мс")
    a1.set_ylabel("% вимірювань")
    a1.set_title("Запізнілі (поза порядком) вимірювання")
    a2.plot(x, [r["p95"] for r in rows], color=RAMP["p95"], marker="o", ms=4, label="p95")
    a2.plot(x, [r["mean"] for r in rows], color=RAMP["mean"], marker="o", ms=4, label="середнє")
    a2.set_xlabel("затримка буфера впорядкування, мс")
    a2.set_ylabel("мс")
    a2.set_title("Затримка генерація → стан")
    a2.legend(loc="upper left")
    save(fig, "fig_reorder.png")


# ---------------------------------------------------------------------------------------------------
def steady_throughput(run):
    """Median processed msg/s over the seconds when data was arriving (edges excluded)."""
    rows = [r for r in run["rows"] if r["ingress_mps"]]
    core = rows[2:-2] or rows
    vals = sorted(r["throughput_mps"] for r in core)
    return vals[len(vals) // 2] if vals else 0.0


def scenario_row(run):
    s = run["summary"]
    e, n, p = s["e2e_ms"], s["net_ms"], s["proc_ms"]
    rows = [r for r in run["rows"] if r["ingress_mps"]]
    return {"run": run["id"], "processed": s.get("processed", 0), "offered_mps": offered_mps(run),
            "avg_mps": sum(r["throughput_mps"] for r in rows) / max(len(rows), 1),
            "peak_mps": max((r["throughput_mps"] for r in rows), default=0),
            "e2e": e, "net": n, "proc": p,
            "sync_threshold": s.get("sync_threshold_ms"), "sync_event": s.get("sync_event_ms"),
            "invalid": s.get("invalid", 0), "duplicate": s.get("duplicate", 0), "late": s.get("late", 0),
            "lost": s.get("lost", 0), "outlier": s.get("outlier", 0), "events": s.get("events", 0),
            "commits": s["commit_counts"], "corrections": s.get("resample_corrections", 0),
            "resampled": s.get("resampled_points", 0), "writer": s.get("writer"),
            "emulators": {k: dict(v) for k, v in run["emulators"].items()}}


def main():
    tables, analysis = [], {}
    hist = History()
    runs = {rid: load_run(rid) for rid in ("S1_normal", "S2_burst", "S3_outage")}
    runs = {k: v for k, v in runs.items() if v}

    if runs:
        tables.append("## Затримка та пропускна здатність за сценаріями\n")
        tables.append("| Сценарій | Оброблено | Сер. потік, пов./с | Пік, пов./с | e2e сер., мс | p50 | p95 | p99 | макс | мережа сер., мс | обробка сер., мс |")
        tables.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for rid, run in runs.items():
            r = scenario_row(run)
            analysis[rid] = r
            e = r["e2e"]
            tables.append(f"| {rid} | {r['processed']} | {r['avg_mps']:.0f} | {r['peak_mps']:.0f} | {fmt(e['mean'])} | "
                          f"{fmt(e['p50'])} | {fmt(e['p95'])} | {fmt(e['p99'])} | {fmt(e['max'])} | "
                          f"{fmt(r['net']['mean'])} | {fmt(r['proc']['mean'])} |")
        tables.append("\n## Якість даних і синхронізація за сценаріями\n")
        tables.append("| Сценарій | Невалідні | Дублі | Запізнілі | Втрачені (seq) | Викиди | Події | Commit PERIODIC | EVENT | THRESHOLD | THRESHOLD сер. затримка, мс | Корекції ресемплінгу |")
        tables.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for rid, r in analysis.items():
            c = r["commits"]
            st = r["sync_threshold"] or {}
            tables.append(f"| {rid} | {r['invalid']} | {r['duplicate']} | {r['late']} | {r['lost']} | {r['outlier']} | "
                          f"{r['events']} | {c['PERIODIC']} | {c['EVENT']} | {c['THRESHOLD']} | {fmt(st.get('mean'))} | {r['corrections']} |")

    if "S1_normal" in runs:
        s1 = runs["S1_normal"]
        fig_timeline(s1, "fig_s1_timeline.png", "Штатний режим")
        fig_latency_hist(s1, "fig_s1_latency_hist.png")
        fig_sync_counts(s1)
        analysis["deadband"] = deadband_tradeoff(s1, hist)
        tables.append("\n## Компроміс порогу зміни (відтворення S1, printer_01)\n")
        tables.append("| Метрика | Поріг | Оновлень THRESHOLD / с | Сер. відхилення | Макс. відхилення |")
        tables.append("|---|---|---|---|---|")
        for m, d in analysis["deadband"].items():
            for p in d["points"]:
                tables.append(f"| {m} | {p['deadband']:g} | {p['updates_per_s']:.2f} | {p['mean_err']:.3f} | {p['max_err']:.3f} |")
    if "S2_burst" in runs:
        fig_timeline(runs["S2_burst"], "fig_s2_burst.png", "Сплеск інтенсивності",
                     phases=[(30, 50, "сплеск ×25"), (80, 100, "сплеск ×60")], log_latency=True)
    if "S3_outage" in runs:
        analysis["S3_detail"] = fig_s3(runs["S3_outage"], hist)

    levels = {"db": [], "nodb": []}
    for d in sorted(RESULTS.glob("L_*")):
        run = load_run(d.name)
        if not run:
            continue
        key = "db" if d.name.startswith("L_db") else "nodb"
        e = run["summary"]["e2e_ms"]
        levels[key].append({"run": d.name, "level": int(d.name.split("_x")[1]), "offered": offered_mps(run),
                            "throughput": steady_throughput(run), "p50": e["p50"], "p95": e["p95"], "max": e["max"],
                            "processed": run["summary"].get("processed", 0),
                            "lagged": sum(s.get("lagged_resets", 0) for s in run["emulators"].values())})
    if levels["db"] or levels["nodb"]:
        fig_load(levels)
        analysis["load_sweep"] = levels
        tables.append("\n## Пропускна здатність: навантажувальний тест (4 принтери, 15 с на рівень)\n")
        tables.append("| Сховище | Множник | Запропоновано, пов./с | Оброблено (медіана), пов./с | e2e p50, мс | p95 | макс |")
        tables.append("|---|---|---|---|---|---|---|")
        for key, lab in (("db", "InfluxDB"), ("nodb", "без запису")):
            for p in sorted(levels[key], key=lambda p: p["level"]):
                tables.append(f"| {lab} | ×{p['level']} | {p['offered']:.0f} | {p['throughput']:.0f} | {fmt(p['p50'])} | "
                              f"{fmt(p['p95'])} | {fmt(p['max'])} |")

    reorder = []
    for d in RESULTS.glob("R_*ms"):
        run = load_run(d.name)
        if not run:
            continue
        s = run["summary"]
        total = s.get("processed", 0) + s.get("late", 0)
        reorder.append({"delay": int(d.name[2:-2]), "late": s.get("late", 0), "late_pct": 100 * s.get("late", 0) / max(total, 1),
                        "mean": s["e2e_ms"]["mean"], "p95": s["e2e_ms"]["p95"], "max": s["e2e_ms"]["max"],
                        "jittered": sum(e.get("jittered", 0) for e in run["emulators"].values()),
                        "sent": sum(e.get("sent", 0) for e in run["emulators"].values())})
    if reorder:
        reorder.sort(key=lambda r: r["delay"])
        fig_reorder(reorder)
        analysis["reorder_sweep"] = reorder
        tables.append("\n## Буфер упорядкування (30 % повідомлень затримано в каналі на 0–300 мс)\n")
        tables.append("| Затримка буфера, мс | Запізнілих, шт. | Запізнілих, % | e2e сер., мс | p95 | макс |")
        tables.append("|---|---|---|---|---|---|")
        for r in reorder:
            tables.append(f"| {r['delay']} | {r['late']} | {r['late_pct']:.2f} | {fmt(r['mean'])} | {fmt(r['p95'])} | {fmt(r['max'])} |")

    (RESULTS / "tables.md").write_text("\n".join(tables) + "\n")
    (RESULTS / "analysis.json").write_text(json.dumps(analysis, indent=2, default=str, ensure_ascii=False))
    print("tables -> results/tables.md")
    hist.close()


if __name__ == "__main__":
    main()
