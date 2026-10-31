"""Preprocessing stage: outlier filtering and resampling to a common grid with gap filling."""
import math
from collections import deque
from dataclasses import dataclass

from .config import METRICS, HampelSpec, PipelineConfig


class HampelFilter:
    """Causal Hampel filter.

    A sample x is an outlier if |x - median(W)| > k * max(1.4826 * MAD(W), min_sigma), where W are the
    last `window` accepted samples. An outlier is replaced by the last accepted value (not by the window
    median: on a trend the median lags behind and produced false alarms). If `persistence` consecutive
    outliers deviate to the same side, the signal changed its level (not a spike): the window is reset
    to the recent raw samples and the current value is accepted.
    """

    def __init__(self, spec: HampelSpec):
        self.spec = spec
        self.window: deque[float] = deque(maxlen=spec.window)
        self._pending: list[float] = []   # consecutive one-sided outliers
        self._side = 0
        self.last: float | None = None     # last accepted value

    def update(self, x: float) -> tuple[float, bool, float]:
        """Returns (filtered value, is_outlier, median)."""
        w = self.window
        if len(w) < 3:          # warm-up: the median of 3 samples is already robust to one spike
            w.append(x)
            self.last = x
            return x, False, x
        s = sorted(w)
        med = _median(s)
        sigma = max(1.4826 * _median(sorted(abs(v - med) for v in s)), self.spec.min_sigma)
        dev = x - med
        if abs(dev) <= self.spec.k * sigma:
            self._pending.clear()
            w.append(x)
            self.last = x
            return x, False, med
        side = 1 if dev > 0 else -1
        if side != self._side:
            self._pending.clear()
        self._side = side
        self._pending.append(x)
        if len(self._pending) >= self.spec.persistence:
            w.clear()
            w.extend(self._pending)
            self._pending.clear()
            self.last = x
            return x, False, med       # level shift, not an outlier
        return self.last, True, med


def _median(sorted_vals: list[float]) -> float:
    n = len(sorted_vals)
    mid = n // 2
    return sorted_vals[mid] if n % 2 else 0.5 * (sorted_vals[mid - 1] + sorted_vals[mid])


@dataclass
class ResampledPoint:
    printer_id: str
    metric: str
    bucket: int          # bucket index, bucket start time = bucket * period
    fill: str            # measured | locf | interpolated | missing
    mean: float | None
    vmin: float | None = None
    vmax: float | None = None
    count: int = 0
    correction: bool = False   # re-emitted value for an already written bucket


class _Agg:
    __slots__ = ("sum", "count", "min", "max")

    def __init__(self):
        self.sum, self.count, self.min, self.max = 0.0, 0, math.inf, -math.inf

    def add(self, v: float):
        self.sum += v
        self.count += 1
        self.min = min(self.min, v)
        self.max = max(self.max, v)

    @property
    def mean(self) -> float:
        return self.sum / self.count


class StreamResampler:
    """Resamples one (printer, metric) stream to buckets of `period` seconds.

    High-rate signals are downsampled (mean/min/max per bucket), low-rate ones are aligned to the grid.
    Empty buckets are filled provisionally by LOCF (up to max_locf); when the gap closes and is not
    longer than max_interp, the filled buckets are re-emitted with linearly interpolated values.
    Samples that arrive for an already closed bucket (late / store-and-forward data) within the
    correction horizon replace the filled value with the measured one. Re-emitted points overwrite the
    previous ones in the TSDB (same series key + timestamp).
    """

    def __init__(self, printer_id: str, metric: str, cfg: PipelineConfig):
        self.pid, self.metric, self.period = printer_id, metric, cfg.resample_period_s
        spec = METRICS.get(metric)
        self.max_locf = round((spec.max_locf_s if spec else cfg.max_locf_s) / self.period)
        self.max_interp = round((spec.max_interp_s if spec else cfg.max_interp_s) / self.period)
        self.horizon = round(cfg.correction_horizon_s / self.period)
        self.max_missing = round(cfg.missing_markers_s / self.period)
        self.open: dict[int, _Agg] = {}
        self.closed: dict[int, tuple[_Agg | None, str, float | None]] = {}  # bucket -> (agg, fill, value)
        self.last_closed: int | None = None
        self.dropped_too_late = 0

    def add(self, ts: float, value: float) -> list[ResampledPoint]:
        k = math.floor(ts / self.period)
        if self.last_closed is None or k > self.last_closed:
            self.open.setdefault(k, _Agg()).add(value)
            return []
        if k <= self.last_closed - self.horizon:
            self.dropped_too_late += 1
            return []
        agg = self.closed.get(k, (None, "", None))[0] or _Agg()
        agg.add(value)
        self.closed[k] = (agg, "measured", agg.mean)
        return [self._point(k, correction=True)] + self._refill(correction=True)

    def close_until(self, k_end: int) -> list[ResampledPoint]:
        """Closes all buckets with index < k_end."""
        if self.last_closed is None:
            if not self.open:
                return []
            self.last_closed = min(self.open) - 1
        out: list[ResampledPoint] = []
        first_new = self.last_closed + 1
        for k in range(first_new, k_end):
            agg = self.open.pop(k, None)
            self.closed[k] = (agg, "measured", agg.mean) if agg else (None, "", None)
            self.last_closed = k
        if k_end <= first_new:
            return out
        out = self._refill(correction=False, new_from=first_new)
        for k in [k for k in self.closed if k <= self.last_closed - self.horizon]:
            del self.closed[k]
        return out

    def _refill(self, correction: bool, new_from: int | None = None) -> list[ResampledPoint]:
        """Recomputes fill method of all closed empty buckets; emits new buckets and changed old ones."""
        out = []
        keys = sorted(self.closed)
        measured = [k for k in keys if self.closed[k][1] == "measured"]
        mi = 0
        for k in keys:
            agg, fill, val = self.closed[k]
            while mi < len(measured) and measured[mi] < k:
                mi += 1
            is_new = new_from is not None and k >= new_from
            if fill == "measured":
                if is_new:
                    out.append(self._point(k))
                continue
            prev = measured[mi - 1] if mi > 0 else None
            nxt = measured[mi] if mi < len(measured) else None
            new_fill, new_val = "missing", None
            if prev is not None and nxt is not None and nxt - prev - 1 <= self.max_interp:
                pv, nv = self.closed[prev][2], self.closed[nxt][2]
                new_fill, new_val = "interpolated", pv + (nv - pv) * (k - prev) / (nxt - prev)
            elif prev is not None and k - prev <= self.max_locf:
                new_fill, new_val = "locf", self.closed[prev][2]
            if prev is None and nxt is None:
                continue  # nothing known about this stream yet
            changed = (new_fill, new_val) != (fill, val)
            self.closed[k] = (None, new_fill, new_val)
            gap = k - prev if prev is not None else 0
            if is_new and (new_fill != "missing" or gap <= self.max_missing):
                out.append(self._point(k))
            elif changed and not is_new and fill != "":
                out.append(self._point(k, correction=True))
        return out

    def _point(self, k: int, correction: bool = False) -> ResampledPoint:
        agg, fill, val = self.closed[k]
        if agg is not None and fill == "measured":
            return ResampledPoint(self.pid, self.metric, k, fill, agg.mean, agg.min, agg.max, agg.count, correction)
        return ResampledPoint(self.pid, self.metric, k, fill, val, None, None, 0, correction)


class Resampler:
    """Holds a StreamResampler per (printer, metric); buckets are closed by the watermark."""

    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self.streams: dict[tuple[str, str], StreamResampler] = {}

    def add(self, printer_id: str, metric: str, ts: float, value: float) -> list[ResampledPoint]:
        s = self.streams.get((printer_id, metric))
        if s is None:
            s = self.streams[(printer_id, metric)] = StreamResampler(printer_id, metric, self.cfg)
        return s.add(ts, value)

    def close(self, watermarks: dict[tuple, float]) -> list[ResampledPoint]:
        """Closes buckets that ended before the stream watermark (event time, see ingest.EventClock)."""
        out = []
        for key, s in self.streams.items():
            wm = watermarks.get(key)
            if wm is not None:
                out += s.close_until(math.floor((wm - self.cfg.resample_grace_s) / self.cfg.resample_period_s))
        return out

    @property
    def dropped_too_late(self) -> int:
        return sum(s.dropped_too_late for s in self.streams.values())
