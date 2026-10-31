"""Historical queries to the time-series storage (demonstration for the lab defence).

  python query_history.py state-at  printer_01 2026-10-31T10:15:00Z   # twin state as it was at that moment
  python query_history.py state-at  printer_01 5m                     # ... 5 minutes ago
  python query_history.py series    printer_01 nozzle_temperature --start 2m [--source raw|resampled|1m] [--every 10s]
  python query_history.py events    printer_01 --start 1h
  python query_history.py stats     printer_01 --start 15m             # aggregates of raw telemetry per metric
  python query_history.py fill      printer_01 --start 15m             # how gaps were filled on the 1 s grid
  python query_history.py flux 'from(bucket: "digital_twin") |> range(start: -1m) |> count()'
Durations (2m, 1h, ...) mean "that long ago".
"""

import argparse
from datetime import datetime

from twin.history import History


def show(rows: list[dict], columns: list[str] | None = None, limit: int = 60):
    if not rows:
        print("(no data)")
        return
    columns = columns or [
        c
        for c in rows[0]
        if not c.startswith("_") or c in ("_time", "_value", "_field")
    ]
    cells = [[_fmt(r.get(c)) for c in columns] for r in rows[:limit]]
    widths = [
        max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)
    ]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more rows (total {len(rows)})")


def _fmt(v) -> str:
    if isinstance(v, datetime):
        return v.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else f"{v:.3f}"
    return "" if v is None else str(v)


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state-at")
    s.add_argument("printer")
    s.add_argument("time", help="RFC3339 timestamp or a duration ago, e.g. 5m")
    s = sub.add_parser("series")
    s.add_argument("printer")
    s.add_argument("metric")
    s.add_argument("--start", default="2m")
    s.add_argument("--stop", default="now()")
    s.add_argument("--source", choices=["resampled", "raw", "1m"], default="resampled")
    s.add_argument("--every", help="additional aggregation window, e.g. 10s")
    for name in ("events", "stats", "fill"):
        s = sub.add_parser(name)
        s.add_argument("printer")
        s.add_argument("--start", default="1h")
    s = sub.add_parser("flux")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=60)
    args = p.parse_args()

    h = History()
    try:
        if args.cmd == "state-at":
            state = h.state_at(args.printer, args.time)
            if not state:
                print("(no state stored before that moment)")
            for k in sorted(state):
                print(f"  {k:26} {_fmt(state[k])}")
        elif args.cmd == "series":
            rows = h.series(
                args.printer,
                args.metric,
                args.start,
                args.stop,
                args.source,
                args.every,
            )
            show(
                rows,
                [
                    c
                    for c in (
                        "_time",
                        "value",
                        "filtered",
                        "outlier",
                        "late",
                        "mean",
                        "count",
                        "fill",
                    )
                    if rows and c in rows[0]
                ],
            )
        elif args.cmd == "events":
            show(
                h.events(args.printer, args.start),
                ["_time", "type", "severity", "message"],
            )
        elif args.cmd == "stats":
            show(
                h.metric_stats(args.printer, args.start),
                ["metric", "count", "mean", "min", "max", "outliers"],
            )
        elif args.cmd == "fill":
            show(h.fill_stats(args.printer, args.start), ["metric", "fill", "_value"])
        elif args.cmd == "flux":
            show(h.query(args.query), limit=args.limit)
    finally:
        h.close()


if __name__ == "__main__":
    main()
