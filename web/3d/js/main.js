// 3D digital twin viewer (lab 5): wiring of data, status rules, scene, information panel and alerts.
//   ?printer=printer_02   select a printer      ?lod=0 / ?shadows=0   switch off LOD / shadows
//   ?bench=1              benchmark with synthetic updates (no live data), results in window.__bench
import { TwinStore, connect } from './twins.js';
import { TwinScene } from './scene.js';
import { Dashboard } from './dashboard.js';
import { Alerts } from './audio.js';
import { FrameStats, runBenchmark } from './bench.js';
import { ELEMENTS, LEVEL_NAME, OK, WARN, CRIT, UNKNOWN, elementLevel, printerLevel } from './status.js';

const params = new URLSearchParams(location.search);
const BENCH = params.has('bench');
const $ = id => document.getElementById(id);

const store = new TwinStore();
const scene = new TwinScene($('scene'), $('labels'), { lod: params.get('lod') !== '0', shadows: params.get('shadows') !== '0' });
const dash = new Dashboard(store, { tiles: $('tiles'), title: $('panel-title'), charts: $('charts'),
                                    periods: $('periods'), events: $('events') });
const alerts = new Alerts();
const hud = new FrameStats(), benchStats = new FrameStats();
const levels = new Map();               // printer -> {node: level}
let printersKey = '', selected = null, popup = null, tilesDirty = false;

await scene.load('models/printer.glb');
scene.addVRButton($('vr-slot'));

const live = () => BENCH || (store.connected && store.ditto);
const printerOf = thingId => thingId.split(':')[1].split('.')[0];
const num = p => p.replace(/^\D+_0*/, '');

// --- state of one printer: levels of all elements -> scene, alerts, event log ----------------------------------
function refreshPrinter(p, announce = true) {
  const link = live(), prev = levels.get(p) ?? {}, cur = {};
  for (const node of Object.keys(ELEMENTS)) {
    const { level, reason } = elementLevel(store, p, node, link);
    cur[node] = level;
    const from = prev[node];
    const known = l => l !== undefined && l !== UNKNOWN;
    if (announce && !BENCH && known(from) && level !== from && (known(level) || node === 'controller')) {
      const temp = store.prop(`kpi.dt:${p}.hotend`, 'heater', 'temperature');
      const extra = node === 'hotend' && typeof temp === 'number' ? `, ${Math.round(temp)} градусів` : '';
      alerts.transition(`${p}/${node}`, from, level, `Принтер ${num(p)}. ${ELEMENTS[node].title}: ${LEVEL_NAME[level]}${extra}.`);
      dash.addEvent(new Date(), level, `${p}: ${ELEMENTS[node].title} — ${LEVEL_NAME[from]} → ${LEVEL_NAME[level]} (${reason})`);
    }
  }
  levels.set(p, cur);
  const overall = printerLevel(store, p, link);
  const pr = (node, f, n) => store.prop(node ? `kpi.dt:${p}.${node}` : `kpi.dt:${p}`, f, n);
  scene.setState(p, cur, overall, {
    link: link ? pr('controller', 'connectivity', 'status') : undefined, mode: pr('', 'status', 'operatingMode'),
    feed: pr('extruder', 'drive', 'feedRate'), feedAvg: pr('extruder', 'drive', 'feedRateAvg5s'),
    extruded: pr('extruder', 'drive', 'extrudedLengthMm'), fan: pr('fan', 'fan', 'speedPercent'),
    temp: pr('hotend', 'heater', 'temperature'),
  }, `<span class="dot lv${overall}"></span>${p} · ${LEVEL_NAME[overall]}`);
  const dot = document.querySelector(`#printers [data-p="${p}"] .dot`);
  if (dot) dot.className = `dot lv${overall}`;
  updateAlarmFrame();
}

function updateAlarmFrame() {
  const any = lv => [...levels.values()].some(m => Object.values(m).includes(lv));
  document.body.classList.toggle('alarm-crit', alerts.critical.size > 0);
  document.body.classList.toggle('alarm-warn', alerts.critical.size === 0 && (any(WARN) || any(CRIT)));
  $('btn-ack').hidden = alerts.critical.size === 0;
}
alerts.onChange = updateAlarmFrame;

function selectPrinter(p, focus = true) {
  selected = p;
  [...$('printers').children].forEach(b => b.classList.toggle('active', b.dataset.p === p));
  dash.setPrinter(p, live());
  if (!BENCH) dash.loadEvents(p);
  if (focus) scene.focus(p);
}

function syncPrinters() {
  const printers = store.printers();
  const key = printers.join(',');
  if (key !== printersKey) {
    printersKey = key;
    scene.setPrinters(printers);
    $('printers').innerHTML = printers.map(p => `<button data-p="${p}"><span class="dot"></span>${p}</button>`).join('');
    if (!printers.includes(selected)) selectPrinter(params.get('printer') && printers.includes(params.get('printer'))
                                                    ? params.get('printer') : printers[0], false);
    scene.overview();
  }
  printers.forEach(p => refreshPrinter(p, false));
}

// --- data events ----------------------------------------------------------------------------------------------
store.on(e => {
  if (e.type === 'snapshot') { syncPrinters(); tilesDirty = true; }
  else if (e.type === 'link') {
    $('link').textContent = live() ? 'Ditto: дані в реальному часі' : 'немає даних з платформи';
    $('link').className = `link ${live() ? 'ok' : 'bad'}`;
    store.printers().forEach(p => refreshPrinter(p));
    tilesDirty = true;
  } else if (e.type === 'change') {
    hud.updates++; benchStats.updates++;
    const p = printerOf(e.thingId);
    if (!levels.has(p)) { syncPrinters(); return; }
    refreshPrinter(p);
    scene.flashLed(p);
    if (p === selected) { dash.onChange(e.thingId, e.changed); tilesDirty = true; }
  }
});
setInterval(() => {                        // the panel is re-rendered at most 4 times per second
  if (tilesDirty && selected) { tilesDirty = false; dash.renderTiles(live()); }
  if (popup) $('popup').innerHTML = dash.renderPopup(popup.printer, popup.node, live());
}, 250);

$('printers').addEventListener('click', e => { const b = e.target.closest('button'); if (b) selectPrinter(b.dataset.p); });

// --- picking, popup -----------------------------------------------------------------------------------------------
scene.onPick = (hit) => {
  popup = hit && ELEMENTS[hit.node] ? hit : null;
  $('popup').hidden = !popup;
  if (popup) {
    $('popup').innerHTML = dash.renderPopup(popup.printer, popup.node, live());
    if (popup.printer !== selected) selectPrinter(popup.printer, false);
  }
};
$('popup').addEventListener('click', e => { if (e.target.dataset.close !== undefined) { popup = null; scene.selected = null; $('popup').hidden = true; } });
window.addEventListener('keydown', e => {
  if (e.key === 'Escape') { popup = null; scene.selected = null; $('popup').hidden = true; }
  if (e.key === 'f' && popup) scene.focus(popup.printer, popup.node);
});

scene.onFrame = (dt, cpuMs) => {
  hud.frame(cpuMs); benchStats.frame(cpuMs);
  if (popup) {
    const at = scene.anchorOf(popup.printer, popup.node);
    $('popup').style.display = at ? '' : 'none';
    if (at) { $('popup').style.left = `${at[0]}px`; $('popup').style.top = `${at[1]}px`; }
    const lv = levels.get(popup.printer)?.[popup.node] ?? UNKNOWN;
    $('popup').style.setProperty('--lv', `var(--${['ok', 'warn', 'crit', 'unknown'][lv]})`);
  }
};

setInterval(() => {                        // HUD: frame rate, frame time, draw calls, update rate, delivery latency
  const s = hud.summary(), r = scene.stats(), lat = [...store.latencies].sort((a, b) => a - b);
  $('hud').textContent = `${s.fps.toFixed(0)} кадр/с   кадр p95 ${s.frameP95.toFixed(1)} мс\n` +
    `CPU сцени ${s.cpuMean.toFixed(2)} мс\nвиклики ${r.calls}   трикутники ${r.triangles}\n` +
    `оновлень двійників ${s.updatesPerSec.toFixed(0)}/с` + (lat.length ? `\nзатримка доставки ${lat[lat.length >> 1].toFixed(0)} мс` : '');
  hud.reset();
}, 1000);

// --- buttons ----------------------------------------------------------------------------------------------------
$('btn-mode').addEventListener('click', () => {
  scene.setMode(scene.mode === 'status' ? 'real' : 'status');
  $('btn-mode').textContent = scene.mode === 'status' ? 'Режим: стан' : 'Режим: матеріали';
});
$('btn-sound').addEventListener('click', () => {
  alerts.enable(!alerts.enabled);
  $('btn-sound').textContent = alerts.enabled ? '🔊 Звук увімкнено' : '🔇 Звук вимкнено';
  $('btn-sound').classList.toggle('active', alerts.enabled);
});
$('btn-ack').addEventListener('click', () => alerts.acknowledge());
$('btn-reset').addEventListener('click', () => scene.overview());

// --- start ------------------------------------------------------------------------------------------------------
if (BENCH) {
  const box = document.createElement('div');
  box.id = 'bench';
  $('scene-wrap').appendChild(box);
  const plan = JSON.parse(params.get('plan') || 'null') ?? [
    { printers: 3, rate: 0 }, { printers: 3, rate: 10 }, { printers: 3, rate: 100 }, { printers: 3, rate: 1000 },
    { printers: 3, rate: 5000 }, { printers: 12, rate: 100 }, { printers: 48, rate: 100 }, { printers: 48, rate: 1000 },
  ];
  store.connected = store.ditto = true;
  runBenchmark({ store, scene, stats: benchStats, plan, log: t => { box.textContent += t + '\n'; } });
} else {
  connect(store);
}
window.__twin = { store, scene, dash, alerts };
