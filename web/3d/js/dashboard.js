// Information panel next to the scene: key indicators, time series from the storage of lab 3, event log, popup.
import uPlot from 'uplot';
import { ELEMENTS, LABELS, LEVEL_COLOR, LEVEL_ICON, LEVEL_NAME, UNKNOWN, elementLevel, fmt, thingId } from './status.js';

// [title, twin node ("" = printer), feature, property, element whose level colors the tile]
const TILES = [
  ['Сопло', 'hotend', 'heater', 'temperature', 'hotend'],
  ['Індекс справності', '', 'status', 'healthIndex', 'frame'],
  ['Режим роботи', '', 'status', 'operatingMode', null],
  ['Подача', 'extruder', 'drive', 'feedRate', 'extruder'],
  ['Струм осі X, RMS', 'axis-x', 'motor', 'currentRms5s', 'axis-x'],
  ['Стіл', 'bed', 'temperature', 'temperature', 'bed'],
  ['Камера', 'enclosure', 'temperature', 'temperature', 'enclosure'],
  ['Вентилятор', 'fan', 'fan', 'speedPercent', 'fan'],
  ['Зв\'язок', 'controller', 'connectivity', 'status', 'controller'],
  ['Тривога', '', 'alarms', 'level', 'frame'],
];

// one value axis per chart; two series only when they share the unit
const CHARTS = [
  { title: 'Температура сопла, °C', bands: [[205, 215, '#e8890c'], [195, 225, '#e03131']],
    series: [{ metric: 'nozzle_temperature', label: 'сопло', color: '#1c7ed6', live: ['hotend', 'heater', 'temperature'] }] },
  { title: 'Стіл і камера, °C',
    series: [{ metric: 'bed_temperature', label: 'стіл', color: '#7048e8', live: ['bed', 'temperature', 'temperature'] },
             { metric: 'chamber_temperature', label: 'камера', color: '#0c8599', live: ['enclosure', 'temperature', 'temperature'] }] },
  { title: 'Подача філаменту, мм/с',
    series: [{ metric: 'extruder_feed_rate', label: 'подача', color: '#1c7ed6', live: ['extruder', 'drive', 'feedRate'] }] },
  { title: 'Струм двигуна осі X, А',
    series: [{ metric: 'stepper_current_x', label: 'струм', color: '#1c7ed6', live: ['axis-x', 'motor', 'current'] }] },
];

export const PERIODS = { '5 хв': ['-5m', null, 300], '15 хв': ['-15m', '5s', 900], '1 год': ['-1h', '15s', 3600], '6 год': ['-6h', '1m', 21600] };

export class Dashboard {
  constructor(store, els) {
    this.store = store;
    this.els = els;
    this.printer = null;
    this.period = '15 хв';
    this.charts = [];
    this.dirty = false;
    this._buildPeriods();
    this._buildCharts();
    setInterval(() => this._redraw(), 1000);       // charts are redrawn at most once per second
    new ResizeObserver(() => this._resize()).observe(els.charts);
  }

  // --- indicators ---------------------------------------------------------------------------------------------
  setPrinter(printer, link) {
    this.printer = printer;
    this.els.title.textContent = `Принтер ${printer}`;
    this.renderTiles(link);
    this.loadHistory();
  }

  renderTiles(link) {
    const p = this.printer, s = this.store;
    if (!p) return;
    this.els.tiles.innerHTML = TILES.map(([title, node, feature, prop, el]) => {
      const twin = node ? `kpi.dt:${p}.${node}` : `kpi.dt:${p}`;
      const v = s.prop(twin, feature, prop);
      const { level, reason } = el ? elementLevel(s, p, el, link) : { level: null, reason: '' };
      const [, unit] = LABELS[prop] ?? [];
      const num = typeof v === 'number' ? fmt(prop, v).replace(` ${unit}`, '') : (v ?? '—');
      const status = level === null ? '' :
        `<div class="s lv${level}"><span class="ico">${LEVEL_ICON[level]}</span>${LEVEL_NAME[level]} · ${reason}</div>`;
      return `<div class="tile ${level === null ? '' : 'lv' + level}"><div class="k">${title}</div>
              <div class="v">${num}${typeof v === 'number' && unit ? `<small>${unit}</small>` : ''}</div>${status}</div>`;
    }).join('');
  }

  // --- popup of a selected element ----------------------------------------------------------------------------
  renderPopup(printer, node, link) {
    const el = ELEMENTS[node];
    if (!el) return '';
    const id = thingId(printer, node), thing = this.store.get(id) ?? {};
    const { level, reason } = elementLevel(this.store, printer, node, link);
    const props = el.feature ? thing.features?.[el.feature]?.properties ?? {} : thing.attributes ?? {};
    const rows = Object.entries(props).filter(([k]) => k !== 'relations' && k !== 'name' && k !== 'kind')
      .map(([k, v]) => `<tr><td>${LABELS[k]?.[0] ?? k}</td><td>${fmt(k, v)}</td></tr>`).join('');
    const rel = Object.entries(thing.attributes?.relations ?? {})
      .map(([r, t]) => `${r} → ${t.split(':')[1]}`).join('<br>');
    return `<button class="close" data-close>✕</button><h3>${el.title}</h3><div class="sub">${id}</div>
      <div class="s lv${level}" style="color:var(--lv)"><b>${LEVEL_ICON[level]} ${LEVEL_NAME[level]}</b> · ${reason}</div>
      <table>${rows || '<tr><td>немає властивостей</td><td></td></tr>'}</table>
      ${rel ? `<div class="rel">${rel}</div>` : ''}`;
  }

  // --- events -------------------------------------------------------------------------------------------------
  addEvent(time, level, text) {
    const li = document.createElement('li');
    li.className = `lv${level}`;
    li.innerHTML = `<time>${time.toLocaleTimeString('uk-UA')}</time>${LEVEL_ICON[level]} ${text}`;
    this.els.events.prepend(li);
    while (this.els.events.children.length > 80) this.els.events.lastChild.remove();
  }

  async loadEvents(printer) {        // recent alarm transitions stored by the pipeline (InfluxDB, measurement twin_events)
    try {
      const rows = await (await fetch(`/api/history/events?printer=${printer}&start=-30m&limit=30`)).json();
      const sev = { info: 0, warning: 1, critical: 2 };
      rows.filter(r => ['THERMAL_STATE', 'MOTOR_LOAD', 'CONNECTIVITY'].includes(r.type))
        .reverse().forEach(r => this.addEvent(new Date(String(r._time).replace(' ', 'T')), sev[r.severity] ?? 0,
                                                `${printer}: ${r.message ?? r.type} (зі сховища)`));
    } catch { /* storage is optional */ }
  }

  // --- time series --------------------------------------------------------------------------------------------
  _buildPeriods() {
    this.els.periods.innerHTML = Object.keys(PERIODS).map(k => `<button data-p="${k}">${k}</button>`).join('');
    this.els.periods.addEventListener('click', e => {
      if (!e.target.dataset.p) return;
      this.period = e.target.dataset.p;
      this.loadHistory();
    });
  }

  _buildCharts() {
    for (const c of CHARTS) {
      const box = document.createElement('div');
      box.className = 'chart';
      box.innerHTML = `<div class="title">${c.title}</div>`;
      this.els.charts.appendChild(box);
      const opts = {
        width: this.els.charts.clientWidth || 400, height: 130,
        scales: { x: { time: true } },
        legend: { show: c.series.length > 1, live: true },
        cursor: { drag: { x: false, y: false } },
        axes: [{ stroke: '#868b94', grid: { stroke: '#eef0f3' }, ticks: { show: false },
                 values: (u, ticks) => ticks.map(t => new Date(t * 1000).toLocaleTimeString('uk-UA',
                   ticks.length > 1 && ticks[1] - ticks[0] < 60 ? {} : { hour: '2-digit', minute: '2-digit' })) },
               { stroke: '#868b94', grid: { stroke: '#eef0f3' }, ticks: { show: false }, size: 42 }],
        series: [{ label: 'час', value: (u, t) => (t == null ? '—' : new Date(t * 1000).toLocaleTimeString('uk-UA')) },
                 ...c.series.map(s => ({ label: s.label, stroke: s.color, width: 2, points: { show: false },
                                              value: (u, v) => (v == null ? '—' : v.toFixed(2)) }))],
        hooks: { draw: [u => this._bands(u, c.bands)] },
      };
      const chart = { def: c, plot: new uPlot(opts, [[], ...c.series.map(() => [])], box), data: c.series.map(() => [[], []]) };
      this.charts.push(chart);
    }
  }

  _bands(u, bands) {               // warning / critical limits of the thermal state (setpoint 210 C +- 5 / 15)
    if (!bands) return;
    const ctx = u.ctx;
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = 1;
    for (const [lo, hi, color] of bands) {
      ctx.strokeStyle = color;
      for (const v of [lo, hi]) {
        const y = u.valToPos(v, 'y', true);
        if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) continue;
        ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
      }
    }
    ctx.restore();
  }

  async loadHistory() {
    const p = this.printer;
    if (!p) return;
    [...this.els.periods.children].forEach(b => b.classList.toggle('active', b.dataset.p === this.period));
    const [start, every] = PERIODS[this.period];
    await Promise.all(this.charts.map(async ch => {
      ch.data = await Promise.all(ch.def.series.map(async s => {
        try {
          // the time axis is the event time of the device; if the device clock runs ahead of the server, its latest
          // points lie "in the future" for the storage - do not cut the range at now(), or they would be missing
          // and the live points (device time) would not join the history
          const stop = new Date(Date.now() + 3600e3).toISOString().replace(/\.\d+Z$/, 'Z');
          const q = new URLSearchParams({ printer: p, metric: s.metric, start, stop, ...(every ? { every } : {}) });
          const rows = await (await fetch(`/api/history/series?${q}`)).json();
          const xs = [], ys = [];
          for (const r of rows) {
            if (r.mean == null) continue;
            xs.push(Date.parse(String(r._time).replace(' ', 'T')) / 1000);
            ys.push(r.mean);
          }
          return [xs, ys];
        } catch { return [[], []]; }
      }));
    }));
    this.dirty = true;
    this._redraw();
  }

  // live points from the twin changes of the selected printer
  onChange(thingIdChanged, changed) {
    const p = this.printer;
    if (!p) return;
    for (const ch of this.charts) {
      ch.def.series.forEach((s, i) => {
        const [node, feature, prop] = s.live;
        if (thingIdChanged !== `kpi.dt:${p}.${node}` || !changed.includes(`/features/${feature}/properties/${prop}`)) return;
        const v = this.store.prop(thingIdChanged, feature, prop);
        const at = this.store.prop(thingIdChanged, feature, 'measuredAt');
        if (typeof v !== 'number') return;
        const x = (at ? Date.parse(at) : Date.now()) / 1000;
        const [xs, ys] = ch.data[i];
        if (xs.length && x <= xs.at(-1)) return;
        xs.push(x); ys.push(v);
        const keep = x - PERIODS[this.period][2];
        while (xs.length && xs[0] < keep) { xs.shift(); ys.shift(); }
        this.dirty = true;
      });
    }
  }

  _redraw() {
    if (!this.dirty) return;
    this.dirty = false;
    for (const ch of this.charts) {
      const aligned = ch.data.length === 1 ? ch.data[0] : uPlot.join(ch.data);
      ch.plot.setData(aligned);
    }
  }

  _resize() {
    const w = this.els.charts.clientWidth;
    for (const ch of this.charts) ch.plot.setSize({ width: w, height: 130 });
  }
}
