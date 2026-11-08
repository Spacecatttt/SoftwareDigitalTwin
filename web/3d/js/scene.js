// 3D scene of the print farm (Three.js / WebGL): one instance of the glTF printer per printer twin.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { VRButton } from 'three/addons/webxr/VRButton.js';
import { ELEMENTS, LEVEL_COLOR, OK, WARN, CRIT, UNKNOWN } from './status.js';

const SPACING = 0.9;                       // m between printers in the row
const LOD_DISTANCE = 6;                    // m: farther printers are drawn as a single colored box
const NODES = new Set(Object.keys(ELEMENTS));
const DECOR = new Set(['print_part', 'controller_led']);
const COLORS = LEVEL_COLOR.map(c => new THREE.Color(c));
const GRAY = new THREE.Color('#9aa0a6');
const HEAT = new THREE.Color('#ff5a14');
const SELECT = new THREE.Color('#1c7ed6');
const LED = { ONLINE: new THREE.Color('#38d96b'), STALE: new THREE.Color('#ffb020'), OFFLINE: new THREE.Color('#ff3030') };

export class TwinScene {
  constructor(canvas, labelsEl, { lod = true, shadows = true } = {}) {
    this.canvas = canvas;
    this.labelsEl = labelsEl;
    this.useLod = lod;
    this.mode = 'status';                  // 'status': colors by level, 'real': materials + warning glow
    this.instances = new Map();            // printer id -> instance
    this.selected = null;                  // {printer, node}
    this.onPick = () => {};
    this.onFrame = () => {};

    const r = this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    r.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    r.outputColorSpace = THREE.SRGBColorSpace;
    r.toneMapping = THREE.ACESFilmicToneMapping;
    r.shadowMap.enabled = shadows;
    r.shadowMap.type = THREE.PCFSoftShadowMap;
    r.xr.enabled = true;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#eceef2');
    const pmrem = new THREE.PMREMGenerator(r);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;   // reflections on metal parts

    this.camera = new THREE.PerspectiveCamera(42, 1, 0.01, 100);
    this.camera.position.set(0.9, 0.75, 1.35);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.minDistance = 0.25;
    this.controls.maxDistance = 30;
    this.controls.target.set(0, 0.3, 0);
    this.tween = null;
    this.follow = null;                    // focused element: the camera follows it (the carriage moves)
    this.controls.addEventListener('start', () => { this.follow = null; this.tween = null; });

    this.scene.add(new THREE.HemisphereLight('#ffffff', '#8a95a5', 0.9));
    const sun = this.sun = new THREE.DirectionalLight('#ffffff', 1.6);
    sun.position.set(1.5, 3, 2);
    sun.castShadow = shadows;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -3, right: 3, top: 3, bottom: -3, near: 0.5, far: 10 });
    this.scene.add(sun, sun.target);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(60, 60), new THREE.ShadowMaterial({ opacity: 0.16 }));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    const grid = new THREE.GridHelper(60, 120, '#c5cad3', '#dde1e7');
    grid.position.y = 0.0005;
    this.scene.add(floor, grid);

    this.raycaster = new THREE.Raycaster();
    this.pickables = [];
    this._bindPointer();
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    this.resize();
    this.clock = new THREE.Clock();
    r.setAnimationLoop(() => this._frame());
  }

  addVRButton(slot) {                         // WebXR: immersive view on a headset (button is disabled otherwise)
    slot.appendChild(VRButton.createButton(this.renderer));
  }

  async load(url) {
    const gltf = await new GLTFLoader().loadAsync(url);
    this.template = gltf.scene;
    this.template.traverse(o => {
      if (o.isMesh) { o.castShadow = o.material.opacity === 1; o.receiveShadow = true; }
    });
    const box = new THREE.Box3().setFromObject(this.template);
    this.size = box.getSize(new THREE.Vector3());
  }

  // --- printer instances -----------------------------------------------------------------------------------
  setPrinters(ids) {
    for (const id of [...this.instances.keys()]) if (!ids.includes(id)) this._remove(id);
    ids.forEach(id => this.instances.has(id) || this._create(id));
    const n = ids.length, cols = n <= 6 ? n : Math.ceil(Math.sqrt(n * 1.5));
    ids.forEach((id, i) => {        // a row, or a grid for large farms (benchmark)
      const row = Math.floor(i / cols), col = i % cols, inRow = Math.min(cols, n - row * cols);
      this.instances.get(id).root.position.set((col - (inRow - 1) / 2) * SPACING, 0, -row * SPACING * 1.2);
    });
    this.pickables = [...this.instances.values()].flatMap(inst => inst.pickables);
  }

  _create(id) {
    const detail = this.template.clone(true);
    const inst = { id, detail, nodes: {}, meshes: {}, pickables: [], levels: {}, data: {}, phase: Math.random() * 6,
                   ledFlash: 0, overall: UNKNOWN };
    detail.traverse(o => {
      if (NODES.has(o.name)) inst.nodes[o.name] = o;
      if (!o.isMesh) return;
      o.material = o.material.clone();                       // colors of every printer are independent
      o.userData.base = { color: o.material.color.clone(), emissive: o.material.emissive.clone(),
                          intensity: o.material.emissiveIntensity, opacity: o.material.opacity };
      let p = o;
      while (p && !NODES.has(p.name)) p = p.parent;
      const node = DECOR.has(o.name) ? null : p?.name;
      o.userData.printer = id;
      o.userData.node = node ?? p?.name;
      if (node) (inst.meshes[node] ??= []).push(o);
      inst.pickables.push(o);
    });
    inst.anim = {
      carriage: detail.getObjectByName('carriage'), rotor: detail.getObjectByName('fan_rotor'),
      gear: detail.getObjectByName('extruder_gear'), led: detail.getObjectByName('controller_led'),
      part: detail.getObjectByName('print_part'),
    };
    // level of detail: the full model near the camera, a box colored by the printer level far away
    const simple = new THREE.Mesh(new THREE.BoxGeometry(this.size.x, this.size.y, this.size.z),
                                  new THREE.MeshStandardMaterial({ color: COLORS[UNKNOWN], roughness: 0.6 }));
    simple.position.y = this.size.y / 2;
    simple.castShadow = true;
    inst.simple = simple;
    if (this.useLod) {
      inst.root = new THREE.LOD();
      inst.root.addLevel(detail, 0);
      inst.root.addLevel(simple, LOD_DISTANCE);
    } else {
      inst.root = new THREE.Group();
      inst.root.add(detail);
    }
    this.scene.add(inst.root);
    inst.label = document.createElement('div');
    inst.label.className = 'plabel';
    this.labelsEl.appendChild(inst.label);
    this.instances.set(id, inst);
  }

  _remove(id) {
    const inst = this.instances.get(id);
    this.scene.remove(inst.root);
    inst.label.remove();
    this.instances.delete(id);
  }

  setState(id, levels, overall, data, labelHtml) {
    const inst = this.instances.get(id);
    if (!inst) return;
    inst.levels = levels;
    inst.overall = overall;
    inst.data = data;
    inst.simple.material.color.copy(COLORS[overall]);
    if (labelHtml !== inst.labelHtml) { inst.label.innerHTML = labelHtml; inst.labelHtml = labelHtml; }
  }

  flashLed(id) { const inst = this.instances.get(id); if (inst) inst.ledFlash = 0.12; }

  setMode(mode) { this.mode = mode; }

  // --- camera ------------------------------------------------------------------------------------------------
  focus(printer, node) {
    const inst = this.instances.get(printer);
    const obj = node && inst?.nodes[node] ? inst.nodes[node] : inst?.root;
    if (!obj) return;
    const box = new THREE.Box3().setFromObject(obj);
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.04);
    const dir = new THREE.Vector3(0.12, 0.3, 1).normalize();      // from the front, through the door between the posts
    const dist = Math.max(radius * 4, 0.45);
    this._tweenTo(center, center.clone().add(dir.multiplyScalar(dist)));
    this.follow = node && inst.nodes[node] ? { obj, last: center } : null;
  }

  overview() {
    this.follow = null;
    const n = this.instances.size;
    const w = Math.max(1, Math.min(n, 6)) * SPACING;
    const target = new THREE.Vector3(0, 0.3, n > 6 ? -SPACING * 1.2 : 0);
    this._tweenTo(target, target.clone().add(new THREE.Vector3(0.3 * w, 0.35 + 0.18 * w, 0.55 + 0.55 * w)));
  }

  _tweenTo(target, position) {
    this.tween = { t: 0, from: [this.controls.target.clone(), this.camera.position.clone()], to: [target, position] };
  }

  // --- picking -------------------------------------------------------------------------------------------------
  _bindPointer() {
    let down = null;
    this.canvas.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; });
    this.canvas.addEventListener('pointerup', e => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
      const hit = this._hit(e);
      this.selected = hit;
      this.onPick(hit, false);
    });
    this.canvas.addEventListener('dblclick', e => {
      const hit = this._hit(e);
      if (hit) { this.selected = hit; this.onPick(hit, true); this.focus(hit.printer, hit.node); }
    });
  }

  _hit(e) {
    const r = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const hits = this.raycaster.intersectObjects(this.pickables, false).filter(h => h.object.visible);
    // the glass enclosure surrounds everything: a part seen through the glass wins over the glass itself
    const hit = hits.find(h => h.object.userData.node !== 'enclosure') ?? hits[0];
    return hit ? { printer: hit.object.userData.printer, node: hit.object.userData.node } : null;
  }

  // screen position of the selected element, for the popup panel
  anchorOf(printer, node) {
    const inst = this.instances.get(printer);
    const obj = inst?.nodes[node];
    if (!obj) return null;
    const p = new THREE.Box3().setFromObject(obj).getCenter(new THREE.Vector3()).project(this.camera);
    if (p.z > 1) return null;
    return [(p.x + 1) / 2 * this.canvas.clientWidth, (1 - p.y) / 2 * this.canvas.clientHeight];
  }

  resize() {
    const w = this.canvas.parentElement.clientWidth, h = this.canvas.parentElement.clientHeight;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / Math.max(h, 1);
    this.camera.updateProjectionMatrix();
  }

  // --- frame ----------------------------------------------------------------------------------------------------
  _frame() {
    const t0 = performance.now();
    const dt = Math.min(this.clock.getDelta(), 0.1), t = this.clock.elapsedTime;
    if (this.tween) {
      const tw = this.tween;
      tw.t = Math.min(1, tw.t + dt / 0.6);
      const k = 1 - (1 - tw.t) ** 3;
      this.controls.target.lerpVectors(tw.from[0], tw.to[0], k);
      this.camera.position.lerpVectors(tw.from[1], tw.to[1], k);
      if (tw.t >= 1) this.tween = null;
    }
    if (this.follow && !this.tween) {       // keep the moving element in the center, same viewing offset
      const c = new THREE.Box3().setFromObject(this.follow.obj).getCenter(new THREE.Vector3());
      const delta = c.clone().sub(this.follow.last);
      this.controls.target.add(delta);
      this.camera.position.add(delta);
      this.follow.last = c;
    }
    this.controls.update();
    for (const inst of this.instances.values()) this._animate(inst, dt, t);
    this.renderer.render(this.scene, this.camera);
    this._labels();
    this.onFrame(dt, performance.now() - t0);
  }

  _animate(inst, dt, t) {
    const d = inst.data, a = inst.anim;
    const online = d.link === 'ONLINE';
    if (this.useLod && inst.root.getCurrentLevel() !== 0) return;    // far away: only the box is drawn
    const printing = online && (d.mode === 'PRINTING' || d.mode === 'HIGH_SPEED');
    // carriage moves along X while printing (speed follows the feed rate), parks when idle / offline
    inst.phase += dt * (printing ? 0.8 + (d.feedAvg ?? 0) / 40 : 0);
    const x = printing ? 0.13 * Math.sin(inst.phase) : -0.14;
    a.carriage.position.x += (x - a.carriage.position.x) * Math.min(1, dt * 8);
    if (online) {
      a.rotor.rotation.x += dt * ((d.fan ?? 0) / 100) * 45;
      a.gear.rotation.x -= dt * ((d.feed ?? 0) / 50) * 5;
    }
    a.part.scale.y = 0.08 + 0.92 * (((d.extruded ?? 0) % 6000) / 6000);   // printed object grows with extrusion
    inst.ledFlash = Math.max(0, inst.ledFlash - dt);
    const led = LED[d.link] ?? GRAY;
    a.led.material.emissive.copy(led);
    a.led.material.color.copy(led);
    a.led.material.emissiveIntensity = online ? (inst.ledFlash > 0 ? 4 : 1.2) : 0.8 + 0.8 * Math.sin(t * 6);

    // color coding of every element by its level
    const pulse = { [WARN]: 0.35 + 0.25 * Math.sin(t * 3), [CRIT]: 0.45 + 0.45 * Math.sin(t * 9) };
    const heat = THREE.MathUtils.clamp(((d.temp ?? 0) - 170) / 80, 0, 1);
    for (const [node, meshes] of Object.entries(inst.meshes)) {
      const level = inst.levels[node] ?? UNKNOWN;
      const sel = this.selected && this.selected.printer === inst.id && this.selected.node === node;
      for (const m of meshes) {
        const b = m.userData.base, mat = m.material;
        if (level === UNKNOWN) mat.color.copy(b.color).lerp(GRAY, 0.75);
        else if (this.mode === 'status') {      // large surfaces (frame, glass) are tinted less than the parts
          const big = node === 'frame' || node === 'enclosure';
          mat.color.copy(b.color).lerp(COLORS[level], level === OK ? (big ? 0.22 : 0.4) : (big ? 0.45 : 0.7));
        }
        else mat.color.copy(b.color);
        let em = null, k = 0;
        if (level === WARN || level === CRIT) { em = COLORS[level]; k = pulse[level]; }
        else if (this.mode === 'real' && node === 'hotend' && heat > 0) { em = HEAT; k = heat * 0.9; }
        if (sel) { em = em ?? SELECT; k = Math.max(k, 0.35); }
        if (em) { mat.emissive.copy(em); mat.emissiveIntensity = k; }
        else { mat.emissive.copy(b.emissive); mat.emissiveIntensity = b.intensity; }
        if (b.opacity < 1) mat.opacity = level === WARN || level === CRIT ? b.opacity * 2 : b.opacity;
      }
    }
  }

  _labels() {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight, v = new THREE.Vector3();
    for (const inst of this.instances.values()) {
      v.set(0, this.size.y + 0.04, 0).applyMatrix4(inst.root.matrixWorld).project(this.camera);
      const hidden = v.z > 1 || this.instances.size > 12 && inst.root.getCurrentLevel?.() !== 0;
      inst.label.style.display = hidden ? 'none' : '';
      if (!hidden) inst.label.style.transform = `translate(${(v.x + 1) / 2 * w}px, ${(1 - v.y) / 2 * h}px) translate(-50%, -100%)`;
    }
  }

  stats() {
    const i = this.renderer.info.render;
    return { calls: i.calls, triangles: i.triangles };
  }
}
