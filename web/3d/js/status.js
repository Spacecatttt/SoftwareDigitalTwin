// Status of the 3D elements: which twin drives which node of the model and how its data maps to a level.
// The levels come from the twin (thermalState, loadState, connectivity are computed by the pipeline of lab 3,
// alarms by the monitor of lab 4); the viewer only maps them to colors and does not re-implement the thresholds.

export const OK = 0, WARN = 1, CRIT = 2, UNKNOWN = 3;
export const LEVEL_NAME = ['норма', 'попередження', 'аварія', 'немає даних'];
export const LEVEL_ICON = ['✓', '!', '‼', '?'];
export const LEVEL_COLOR = ['#2f9e44', '#e8890c', '#e03131', '#868e96'];

// node of the glTF model -> feature of the twin kpi.dt:<printer>.<node> ("" = the printer twin itself)
export const ELEMENTS = {
  hotend:     { title: 'Хотенд (нагрівач і сопло)', feature: 'heater' },
  bed:        { title: 'Стіл з підігрівом', feature: 'temperature' },
  enclosure:  { title: 'Корпус (камера)', feature: 'temperature' },
  extruder:   { title: 'Екструдер', feature: 'drive' },
  'axis-x':   { title: 'Вісь X (кроковий двигун)', feature: 'motor' },
  fan:        { title: 'Вентилятор', feature: 'fan' },
  controller: { title: 'Контролер', feature: 'connectivity' },
  psu:        { title: 'Блок живлення', feature: null },
  frame:      { title: 'Принтер (рама)', feature: 'status', twin: '' },
};

// property name -> [label, unit, digits] for the popup and the tiles
export const LABELS = {
  temperature: ['Температура', '°C', 1], temperatureAvg30s: ['Середня за 30 с', '°C', 1],
  temperatureRate: ['Швидкість зміни', '°C/с', 2], thermalState: ['Тепловий стан'], targetTemperature: ['Уставка', '°C', 0],
  feedRate: ['Подача', 'мм/с', 1], feedRateAvg5s: ['Подача, 5 с', 'мм/с', 1], extrudedLengthMm: ['Видавлено', 'мм', 0],
  filamentUsedG: ['Філамент', 'г', 2], current: ['Струм', 'А', 3], currentRms5s: ['RMS струму, 5 с', 'А', 3],
  loadState: ['Навантаження'], speedPercent: ['Оберти', '%', 0], status: ['Зв\'язок'], messageRateHz: ['Повідомлень', '/с', 1],
  operatingMode: ['Режим'], healthIndex: ['Індекс справності', '%', 1], dataQualityPct: ['Якість даних', '%', 1],
  anomalies60s: ['Аномалій за 60 с'], quality: ['Якість'], measuredAt: ['Виміряно'], lastEventAt: ['Остання подія'],
};

const MAP = {
  thermalState: { NORMAL: OK, WARNING: WARN, CRITICAL: CRIT },
  loadState: { NORMAL: OK, HIGH: WARN, OVERLOAD: CRIT },
  status: { ONLINE: OK, STALE: WARN, OFFLINE: CRIT },
  level: { NONE: OK, WARNING: WARN, CRITICAL: CRIT },
  quality: { GOOD: OK, FILTERED: OK, STALE: UNKNOWN, UNKNOWN: UNKNOWN },
};

export const thingId = (printer, node) => (ELEMENTS[node]?.twin === '' ? `kpi.dt:${printer}` : `kpi.dt:${printer}.${node}`);

// Level of one element of one printer. ctx.link === false (viewer has no live data) makes everything UNKNOWN.
export function elementLevel(store, printer, node, link = true) {
  if (!link) return { level: UNKNOWN, reason: 'немає зв\'язку з платформою' };
  const p = (twin, f, n) => store.prop(twin, f, n);
  const ctl = p(`kpi.dt:${printer}.controller`, 'connectivity', 'status');
  if (node === 'controller') {
    const lv = MAP.status[ctl] ?? UNKNOWN;
    return { level: lv, reason: `зв'язок ${ctl ?? '—'}` };
  }
  // the device does not report: its data is outdated, show it as "no data" instead of the last values
  if (ctl === 'OFFLINE' || ctl === 'STALE') return { level: UNKNOWN, reason: `дані застарілі (контролер ${ctl})` };
  const id = thingId(printer, node);
  switch (node) {
    case 'hotend': {
      const s = p(id, 'heater', 'thermalState');
      return { level: MAP.thermalState[s] ?? UNKNOWN, reason: `тепловий стан ${s ?? '—'}` };
    }
    case 'axis-x': {
      const s = p(id, 'motor', 'loadState');
      return { level: MAP.loadState[s] ?? UNKNOWN, reason: `навантаження ${s ?? '—'}` };
    }
    case 'frame': {
      const s = p(id, 'alarms', 'level');
      return { level: MAP.level[s] ?? OK, reason: s && s !== 'NONE' ? `тривога ${s}` : 'тривог немає' };
    }
    case 'psu':
      return { level: OK, reason: 'стан виводиться з контролера' };
    default: {
      const f = ELEMENTS[node]?.feature;
      const q = f ? p(id, f, 'quality') : undefined;
      return { level: MAP.quality[q] ?? UNKNOWN, reason: `якість даних ${q ?? '—'}` };
    }
  }
}

// Overall level of a printer: the worst of its elements and of its alarm
export function printerLevel(store, printer, link = true) {
  let worst = OK, unknown = 0;
  for (const node of Object.keys(ELEMENTS)) {
    const { level } = elementLevel(store, printer, node, link);
    if (level === UNKNOWN) unknown++;
    else worst = Math.max(worst, level);
  }
  return worst === OK && unknown >= 3 ? UNKNOWN : worst;
}

export function fmt(name, v) {
  if (v === undefined || v === null) return '—';
  const [, unit, digits] = LABELS[name] ?? [];
  if (typeof v === 'number') return `${digits !== undefined ? v.toFixed(digits) : v}${unit ? ' ' + unit : ''}`;
  if (name === 'measuredAt' || name === 'lastEventAt' || name === 'syncedAt') return new Date(v).toLocaleTimeString('uk-UA');
  return String(v);
}
