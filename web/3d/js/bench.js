// Performance of the visualization: frame statistics for the HUD and a benchmark with synthetic twin updates.
// The synthetic changes go through the same path as the live ones (TwinStore.apply -> levels -> scene, panel).

export class FrameStats {
  constructor() { this.reset(); }
  reset() { this.intervals = []; this.cpu = []; this.updates = 0; this.last = null; this.since = performance.now(); }
  frame(cpuMs) {
    const now = performance.now();
    if (this.last !== null) this.intervals.push(now - this.last);
    this.last = now;
    this.cpu.push(cpuMs);
  }
  summary() {
    const secs = (performance.now() - this.since) / 1000;
    const iv = [...this.intervals].sort((a, b) => a - b), cpu = [...this.cpu].sort((a, b) => a - b);
    const pct = (a, p) => (a.length ? a[Math.min(a.length - 1, Math.floor(a.length * p))] : 0);
    return {
      fps: iv.length ? 1000 / (iv.reduce((s, v) => s + v, 0) / iv.length) : 0,
      frameP95: pct(iv, 0.95), cpuMean: cpu.length ? cpu.reduce((s, v) => s + v, 0) / cpu.length : 0,
      cpuP95: pct(cpu, 0.95), updatesPerSec: this.updates / Math.max(secs, 1e-3),
    };
  }
}

const NODES = {
  controller: () => ({ connectivity: { properties: { status: 'ONLINE', messageRateHz: 24 } } }),
  hotend: () => ({ heater: { properties: { temperature: 210, thermalState: 'NORMAL', quality: 'GOOD' } } }),
  bed: () => ({ temperature: { properties: { temperature: 60, quality: 'GOOD' } } }),
  enclosure: () => ({ temperature: { properties: { temperature: 40, quality: 'GOOD' } } }),
  extruder: () => ({ drive: { properties: { feedRate: 50, feedRateAvg5s: 50, extrudedLengthMm: 0, quality: 'GOOD' } } }),
  'axis-x': () => ({ motor: { properties: { current: 1.2, currentRms5s: 1.2, loadState: 'NORMAL', quality: 'GOOD' } } }),
  fan: () => ({ fan: { properties: { speedPercent: 80, quality: 'GOOD' } } }),
  psu: () => ({}),
};

export function syntheticFarm(n) {
  const things = [];
  for (let i = 1; i <= n; i++) {
    const p = `bench_${String(i).padStart(2, '0')}`;
    things.push({ thingId: `kpi.dt:${p}`, attributes: { kind: 'Printer' },
                  features: { status: { properties: { operatingMode: 'PRINTING', healthIndex: 90 } },
                              alarms: { properties: { level: 'NONE', active: false } } } });
    for (const [node, f] of Object.entries(NODES)) {
      things.push({ thingId: `kpi.dt:${p}.${node}`, attributes: { kind: node, relations: { isPartOf: `kpi.dt:${p}` } },
                    features: f() });
    }
  }
  return things;
}

// one synthetic change of a random element, shaped like a relayed Ditto event (JSON merge patch at "/")
function randomChange(printers, t) {
  const p = printers[Math.floor(Math.random() * printers.length)];
  const r = Math.random(), noise = () => (Math.random() - 0.5);
  if (r < 0.35) {
    const temp = 210 + 6 * Math.sin(t / 3 + p.length) + noise();
    const s = Math.abs(temp - 210) > 15 ? 'CRITICAL' : Math.abs(temp - 210) > 5 ? 'WARNING' : 'NORMAL';
    return [`kpi.dt:${p}.hotend`, { features: { heater: { properties: { temperature: temp, thermalState: s } } } }];
  }
  if (r < 0.6) return [`kpi.dt:${p}.extruder`, { features: { drive: { properties: { feedRate: 50 + 40 * noise(), extrudedLengthMm: t * 50 } } } }];
  if (r < 0.8) return [`kpi.dt:${p}.axis-x`, { features: { motor: { properties: { current: 1.2 + 0.05 * noise() } } } }];
  if (r < 0.9) return [`kpi.dt:${p}.fan`, { features: { fan: { properties: { speedPercent: 80 + 4 * noise() } } } }];
  return [`kpi.dt:${p}.bed`, { features: { temperature: { properties: { temperature: 60 + noise() } } } }];
}

// Benchmark plan: for each step the farm is rebuilt with `printers` synthetic printers and `rate` changes per second
// are applied for warmup + measure seconds. Results go to window.__bench (read by experiments/viz_bench.py).
export async function runBenchmark({ store, scene, stats, plan, warmup = 2, measure = 6, log }) {
  const gl = scene.renderer.getContext();
  const ext = gl.getExtension('WEBGL_debug_renderer_info');
  const gpu = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  window.__bench = { done: false, gpu, lod: scene.useLod, results: [] };
  log(`GPU: ${gpu}\nLOD: ${scene.useLod ? 'так' : 'ні'}\n`);
  for (const step of plan) {
    store.load(syntheticFarm(step.printers));
    scene.overview();
    const printers = store.printers();
    let acc = 0, t0 = performance.now(), measuring = false;
    await new Promise(resolve => {
      const tick = () => {
        const now = performance.now(), t = (now - t0) / 1000;
        acc += step.rate * Math.min((now - (tick.last ?? now)) / 1000, 0.1);
        tick.last = now;
        for (; acc >= 1; acc--) store.apply(change(printers, t));     // counted by the change listener of main.js
        if (!measuring && t >= warmup) { stats.reset(); measuring = true; }
        if (t >= warmup + measure) return resolve();
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    const s = { ...step, ...stats.summary(), ...scene.stats() };
    window.__bench.results.push(s);
    log(`${String(step.printers).padStart(3)} пр. ${String(step.rate).padStart(5)} онов./с → ` +
        `${s.fps.toFixed(1)} кадр/с, кадр p95 ${s.frameP95.toFixed(1)} мс, CPU ${s.cpuMean.toFixed(2)} мс, ` +
        `${s.calls} викликів, ${s.triangles} трикутників`);
  }
  window.__bench.done = true;
}

function change(printers, t) {
  const [thingId, value] = randomChange(printers, t);
  return { thingId, action: 'merged', path: '/', value };
}
