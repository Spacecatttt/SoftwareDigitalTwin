// Twin state in the browser: a copy of the Ditto twins, kept up to date by the change events relayed by viz_server.py.
// A change event carries the Ditto event action and the changed JSON at a path (JSON merge patch for "merged").

export class TwinStore {
  constructor() {
    this.things = new Map();          // thingId -> Thing JSON (attributes, features)
    this.listeners = new Set();
    this.connected = false;           // browser <-> viz_server.py
    this.ditto = false;               // viz_server.py <-> Ditto
    this.applied = 0;                 // number of applied changes (for the update rate)
    this.latencies = [];              // adapter -> viz_server delivery latency of the last changes, ms
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(e) { for (const fn of this.listeners) fn(e); }

  load(things) {
    this.things.clear();
    for (const t of things) this.things.set(t.thingId, t);
    this.emit({ type: 'snapshot' });
  }

  apply(ev) {
    const { thingId, action, path, value } = ev;
    if (action === 'deleted' && path === '/') {
      this.things.delete(thingId);
    } else {
      const thing = this.things.get(thingId) ?? { thingId };
      const keys = path.split('/').filter(Boolean);
      if (action === 'merged') mergeAt(thing, keys, value);
      else setAt(thing, keys, value);
      this.things.set(thingId, thing);
    }
    this.applied++;
    if (ev.latencyMs != null) { this.latencies.push(ev.latencyMs); if (this.latencies.length > 200) this.latencies.shift(); }
    this.emit({ type: 'change', thingId, changed: leafPaths(path, value) });
  }

  get(thingId) { return this.things.get(thingId); }

  prop(thingId, feature, name) {
    return this.things.get(thingId)?.features?.[feature]?.properties?.[name];
  }

  printers() {
    return [...this.things.values()].filter(t => t.attributes?.kind === 'Printer').map(t => t.thingId.split(':')[1]).sort();
  }
}

function setAt(obj, keys, value) {
  if (!keys.length) { Object.keys(obj).forEach(k => k !== 'thingId' && delete obj[k]); Object.assign(obj, value); return; }
  let o = obj;
  for (const k of keys.slice(0, -1)) o = (o[k] ??= {});
  o[keys.at(-1)] = value;
}

function mergeAt(obj, keys, patch) {      // RFC 7396: objects merge recursively, null deletes
  let o = obj;
  for (const k of keys.slice(0, -1)) o = (o[k] ??= {});
  const last = keys.at(-1);
  if (last === undefined) { mergeObj(obj, patch); return; }
  if (patch === null) delete o[last];
  else if (isObj(patch)) mergeObj((o[last] = isObj(o[last]) ? o[last] : {}), patch);
  else o[last] = patch;
}

function mergeObj(target, patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) delete target[k];
    else if (isObj(v)) mergeObj((target[k] = isObj(target[k]) ? target[k] : {}), v);
    else target[k] = v;
  }
}

const isObj = v => v !== null && typeof v === 'object' && !Array.isArray(v);

function leafPaths(path, value, out = []) {
  if (isObj(value) && Object.keys(value).length) {
    for (const [k, v] of Object.entries(value)) leafPaths(`${path.replace(/\/$/, '')}/${k}`, v, out);
  } else out.push(path);
  return out;
}

// Server-sent events from viz_server.py: "snapshot" (all twins), then "twin" for every change, "status" each second.
export function connect(store, url = '/api/stream') {
  const es = new EventSource(url);
  es.addEventListener('snapshot', e => { store.connected = true; store.load(JSON.parse(e.data)); });
  es.addEventListener('twin', e => store.apply(JSON.parse(e.data)));
  es.addEventListener('status', e => {
    const s = JSON.parse(e.data);
    store.connected = true;
    if (store.ditto !== s.ditto) { store.ditto = s.ditto; store.emit({ type: 'link' }); }
  });
  es.onerror = () => { store.connected = false; store.ditto = false; store.emit({ type: 'link' }); };
  return es;   // EventSource reconnects by itself; a new snapshot arrives after every reconnect
}
