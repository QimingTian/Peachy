import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import URDFLoader from './lib/urdf/URDFLoader.js';
import { calculatePassiveJoints, buildHeadPoseMatrix } from './Kinematics.js';

const HOME_EYE = [0.62, 0.34, 0.5];
const HOME_AT = [0, 0.19, 0];
const BASE = new URL('./', import.meta.url).href;
const HEAD_JOINTS = ['yaw_body', 'stewart_1', 'stewart_2', 'stewart_3', 'stewart_4', 'stewart_5', 'stewart_6'];
const PASSIVE_JOINTS = [];
for (let i = 1; i <= 7; i++) PASSIVE_JOINTS.push(`passive_${i}_x`, `passive_${i}_y`, `passive_${i}_z`);

function urdfColors(text) {
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  const map = {};
  doc.querySelectorAll('visual').forEach(v => {
    const mesh = v.querySelector('geometry mesh');
    const color = v.querySelector('material color');
    const mat = v.querySelector('material');
    if (!mesh || !color) return;
    const [r, g, b, a] = color.getAttribute('rgba').split(' ').map(Number);
    map[mesh.getAttribute('filename').split('/').pop()] = {
      color: new THREE.Color(r, g, b), opacity: a, name: mat?.getAttribute('name') || '',
    };
  });
  return map;
}

function material(file, info) {
  const opacity = info?.opacity ?? 1;
  const glass = opacity < 0.4;
  const dark = glass || file.includes('antenna_V2') || info?.name === 'antenna_material';
  const m = new THREE.MeshPhysicalMaterial({
    color: dark ? 0x151618 : (info?.color ?? 0xd4d5d8),
    metalness: 0, roughness: dark ? 0.08 : 0.72,
    transparent: glass, opacity, side: glass ? THREE.DoubleSide : THREE.FrontSide,
  });
  if (file.includes('link')) { m.color.setHex(0xe6e7ea); m.metalness = 1; m.roughness = 0.32; }
  if (info?.name === 'antenna_material') { m.clearcoat = 1; m.clearcoatRoughness = 0; }
  return m;
}

export class Twin {
  constructor(container, { onState, onLink } = {}) {
    this.el = container;
    this.onState = onState || (() => {});
    this.onLink = onLink || (() => {});
    this.active = false;
    this.robot = null;
    this.joints = {};
    this.ws = null;
    this.wsUrl = '';
    this.lastApply = 0;
    this.pending = null;
    this._setupScene();
    this._ready = this._load();
  }

  _setupScene() {
    const w = this.el.clientWidth || 640, h = this.el.clientHeight || 360;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(34, w / h, 0.01, 50);
    this.camera.position.set(...HOME_EYE);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.setSize(w, h);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.domElement.className = 'twin-canvas';
    this.el.appendChild(this.renderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(...HOME_AT);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.enablePan = false;
    this.controls.minDistance = 0.25;
    this.controls.maxDistance = 1.4;
    this.controls.autoRotate = true;
    this.controls.autoRotateSpeed = 0.6;
    this.controls.addEventListener('start', () => { this.controls.autoRotate = false; });
    this.controls.update();

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x161719, 0.9));
    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(1.2, 1.6, 1.0);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    Object.assign(key.shadow.camera, { near: 0.1, far: 6, left: -0.5, right: 0.5, top: 0.5, bottom: -0.5 });
    key.shadow.bias = -0.0004;
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xb8bcc4, 0.6);
    fill.position.set(-1.4, 0.5, 0.6);
    this.scene.add(fill);
    const rim = new THREE.DirectionalLight(0xffffff, 0.8);
    rim.position.set(0, 1.0, -1.6);
    this.scene.add(rim);

    const ground = new THREE.Mesh(new THREE.PlaneGeometry(3, 3), new THREE.ShadowMaterial({ opacity: 0.45 }));
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    this.scene.add(ground);

    const ring = new THREE.Mesh(
      new THREE.RingGeometry(0.155, 0.158, 128),
      new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.55, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.001;
    this.scene.add(ring);
    const heading = new THREE.Mesh(
      new THREE.PlaneGeometry(0.05, 0.004),
      new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide }));
    heading.rotation.x = -Math.PI / 2;
    heading.position.set(0.18, 0.0012, 0);
    this.heading = new THREE.Group();
    this.heading.add(heading);
    this.scene.add(this.heading);

    this._resize = new ResizeObserver(() => this.resize());
    this._resize.observe(this.el);
  }

  async _load() {
    const text = await (await fetch(BASE + 'assets/reachy-mini.urdf')).text();
    const colors = urdfColors(text);
    const draco = new DRACOLoader();
    draco.setDecoderPath(BASE + 'lib/draco/');
    const gltf = new GLTFLoader();
    gltf.setDRACOLoader(draco);

    const loader = new URDFLoader();
    loader.packages = { assets: BASE + 'assets/', reachy_mini_description: BASE + 'assets/' };
    loader.workingPath = BASE + 'assets/';
    loader.loadMeshCb = (path, _manager, done) => {
      const file = path.split('/').pop();
      gltf.load(BASE + 'assets/meshes_optimized/' + file, res => {
        let geom = null;
        res.scene.traverse(c => { if (c.isMesh && !geom) geom = c.geometry; });
        if (!geom) return done(res.scene);
        const mesh = new THREE.Mesh(geom, material(file, colors[file]));
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        done(mesh);
      }, undefined, err => done(null, err));
    };

    const blob = URL.createObjectURL(new Blob([text], { type: 'application/xml' }));
    const robot = await new Promise((ok, fail) => loader.load(blob, ok, undefined, fail));
    URL.revokeObjectURL(blob);
    robot.rotation.x = -Math.PI / 2;
    robot.traverse(c => { if (c.isURDFJoint) this.joints[c.name] = c; });
    this.robot = robot;
    this.scene.add(robot);
    try {
      const st = await (await fetch(BASE + 'assets/default_state.json')).json();
      this._apply(st, true);
    } catch { }
    if (this.pending) this._apply(this.pending, true);
    this.render();
  }

  _apply(data, force) {
    if (!this.robot) { this.pending = data; return; }
    const now = performance.now();
    if (!force && now - this.lastApply < 33) return;
    this.lastApply = now;
    let pose = null;
    if (data.head_pose) {
      pose = Array.isArray(data.head_pose) && data.head_pose.length === 16 ? data.head_pose
        : data.head_pose.m ? data.head_pose.m : buildHeadPoseMatrix(data.head_pose);
    }
    const head = data.head_joints?.length === 7 ? data.head_joints : [data.body_yaw || 0, 0, 0, 0, 0, 0, 0];
    HEAD_JOINTS.forEach((n, i) => this.joints[n]?.setJointValue(head[i]));
    if (pose) {
      const p = calculatePassiveJoints(head, pose);
      PASSIVE_JOINTS.forEach((n, i) => this.joints[n]?.setJointValue(p[i]));
    }
    if (data.antennas_position?.length >= 2) {
      this.joints.right_antenna?.setJointValue(-data.antennas_position[0]);
      this.joints.left_antenna?.setJointValue(-data.antennas_position[1]);
    }
    this.heading.rotation.y = data.body_yaw || 0;
  }

  connect(host) {
    const url = `ws://${host}:8000/api/state/ws/full?with_head_joints=true&with_body_yaw=true&with_antenna_positions=true`;
    if (url === this.wsUrl && this.ws && this.ws.readyState <= 1) return;
    this.disconnect();
    this.wsUrl = url;
    this._open();
  }

  _open() {
    const ws = new WebSocket(this.wsUrl);
    this.ws = ws;
    ws.onopen = () => this.onLink(true);
    ws.onmessage = e => {
      let d;
      try { d = JSON.parse(e.data); } catch { return; }
      this._apply(d);
      this.onState(d);
    };
    ws.onclose = () => {
      this.onLink(false);
      if (this.ws === ws) this._retry = setTimeout(() => this._open(), 3000);
    };
  }

  disconnect() {
    clearTimeout(this._retry);
    const ws = this.ws;
    this.ws = null;
    this.wsUrl = '';
    if (ws) { ws.onclose = null; ws.close(); this.onLink(false); }
  }

  setActive(on) {
    if (on === this.active) return;
    this.active = on;
    if (on) { this.resize(); this._loop(); }
    else cancelAnimationFrame(this._raf);
  }

  _loop() {
    if (!this.active) return;
    this._raf = requestAnimationFrame(() => this._loop());
    this.render();
  }

  render() {
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
    this.render();
  }

  resetView() {
    this.camera.position.set(...HOME_EYE);
    this.controls.target.set(...HOME_AT);
    this.controls.autoRotate = true;
  }

  get ready() { return this._ready; }
}
