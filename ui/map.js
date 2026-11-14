// Forest map: SVG, smooth pan / zoom / pinch, names shown on hover or zoom, drone animation.
// World metres: x east, y north. Fills and grid live in a scaled layer; markers, labels and
// paths live in a screen-space overlay so they stay crisp and the same size at every zoom.

import { el, icon, reducedMotion, svg } from "./util.js";

const PAD = 24; // px of margin around the forest at "fit"
const MAX_ZOOM = 10; // x fit
const ALL_NAMES_AT_SECTOR_PX = 150; // every node name shows once a sector is this wide
const SECTOR_LABELS_AT_PX = 120;
const AMBER_MS = 12_000;
const RED_MS = 90_000;
const HOVER_DELAY_MS = 120;
const HIT_PX = 18;

const ease = (p) => (p < 0.5 ? 4 * p * p * p : 1 - (-2 * p + 2) ** 3 / 2);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export class ForestMap {
  constructor(root, world, { onSelectNode, onLabelsToggle, showLabels = false } = {}) {
    this.root = root;
    this.world = world;
    this.W = world.width_m;
    this.H = world.height_m;
    this.S = world.sector_m;
    this.onSelectNode = onSelectNode || (() => {});
    this.onLabelsToggle = onLabelsToggle || (() => {});
    this.showLabels = showLabels;
    this.view = { k: 1, tx: 0, ty: 0 };
    this.kFit = 1;
    this.size = { w: 0, h: 0 };
    this.anim = null;
    this.raf = 0;
    this.nodeState = new Map();
    this.hover = null;
    this.selected = null;
    this.pointers = new Map();
    this.labelWidth = new Map();
    this.drone = { from: null, to: null, t0: 0, dur: 0, flying: false, trail: [], path: [] };
    this.incidentRects = new Map();
    this.rainRects = new Map();

    this.build();
    this.bind();
    new ResizeObserver(() => this.resize()).observe(root);
    this.resize();
    setInterval(() => this.expireStates(), 1000);
  }

  // ---------- coordinates ----------

  sx(x) {
    return x * this.view.k + this.view.tx;
  }

  sy(y) {
    return (this.H - y) * this.view.k + this.view.ty;
  }

  toWorld(px, py) {
    return { x: (px - this.view.tx) / this.view.k, y: this.H - (py - this.view.ty) / this.view.k };
  }

  // ---------- DOM ----------

  build() {
    const { W, H, S } = this;
    const cols = this.world.cols;
    const rows = this.world.rows;

    this.svg = svg("svg", {
      class: "map-svg",
      role: "application",
      "aria-label": "Forest map",
      tabindex: 0,
    });
    this.hatch = svg(
      "pattern",
      { id: "nofly-hatch", width: 7, height: 7, patternUnits: "userSpaceOnUse" },
      svg("line", { x1: 0, y1: 0, x2: 0, y2: 7, class: "hatch-line" }),
    );
    this.svg.append(svg("defs", {}, this.hatch));

    // Scaled layer (world metres, y flipped).
    this.gWorld = svg("g", { class: "world" });
    this.gWorld.append(svg("rect", { class: "land", x: 0, y: 0, width: W, height: H }));
    this.gRain = svg("g", { class: "rain-layer" });
    this.gIncidents = svg("g", { class: "incident-layer" });
    const grid = svg("g", { class: "grid" });
    for (let c = 1; c < cols; c++) {
      grid.append(svg("line", { x1: c * S, y1: 0, x2: c * S, y2: H, "vector-effect": "non-scaling-stroke" }));
    }
    for (let r = 1; r < rows; r++) {
      grid.append(svg("line", { x1: 0, y1: r * S, x2: W, y2: r * S, "vector-effect": "non-scaling-stroke" }));
    }
    grid.append(
      svg("rect", { class: "border", x: 0, y: 0, width: W, height: H, "vector-effect": "non-scaling-stroke" }),
    );
    this.gWorld.append(this.gRain, this.gIncidents, grid);

    // Screen-space overlay.
    this.gOverlay = svg("g", { class: "overlay" });
    this.noFly = this.world.no_fly.map((name) => {
      const fill = svg("rect", { class: "nofly-fill", fill: "url(#nofly-hatch)" });
      const edge = svg("rect", { class: "nofly-edge" });
      const label = svg("text", { class: "nofly-label", "text-anchor": "middle", text: "No-fly" });
      this.gOverlay.append(fill, edge, label);
      return { name, rect: this.sectorRect(name), fill, edge, label };
    });

    this.gAxis = svg("g", { class: "axis", "aria-hidden": "true" });
    this.colLabels = [];
    this.rowLabels = [];
    for (let c = 0; c < cols; c++) {
      const t = svg("text", { "text-anchor": "middle", text: String.fromCharCode(65 + c) });
      this.colLabels.push(t);
      this.gAxis.append(t);
    }
    for (let r = 0; r < rows; r++) {
      const t = svg("text", { "text-anchor": "middle", text: String(r + 1) });
      this.rowLabels.push(t);
      this.gAxis.append(t);
    }
    this.gSectorLabels = svg("g", { class: "sector-labels", "aria-hidden": "true" });
    this.sectorLabels = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const t = svg("text", { text: `${String.fromCharCode(65 + c)}${r + 1}` });
        this.sectorLabels.push({ c, r, t });
        this.gSectorLabels.append(t);
      }
    }

    this.planLine = svg("polyline", { class: "drone-plan" });
    this.trailLine = svg("polyline", { class: "drone-trail" });

    this.base = svg(
      "g",
      { class: "base", role: "img", "aria-label": "Base" },
      svg("rect", { x: -6, y: -6, width: 12, height: 12, rx: 3.5 }),
      svg("circle", { r: 2 }),
    );

    this.gNodes = svg("g", { class: "nodes" });
    this.nodes = this.world.nodes.map((n) => {
      const label = svg("text", { class: "label", x: 12, y: 4, text: n.name });
      const g = svg(
        "g",
        { class: "node idle", role: "img", "aria-label": n.name, "data-node-name": n.name, dataset: { nodeId: n.id } },
        svg("circle", { class: "halo", r: 13 }),
        svg("circle", { class: "ring", r: 7.5 }),
        svg("circle", { class: "dot", r: 5 }),
        label,
      );
      this.gNodes.append(g);
      return { node: n, g, label };
    });

    this.droneG = svg(
      "g",
      { class: "drone", role: "img", "aria-label": "Drone" },
      svg("circle", { class: "halo", r: 14 }),
      svg("circle", { class: "body", r: 6.5 }),
    );

    this.gOverlay.append(this.gAxis, this.gSectorLabels, this.planLine, this.trailLine, this.base, this.gNodes, this.droneG);
    this.svg.append(this.gWorld, this.gOverlay);

    this.calloutText = el("div", { class: "callout-bubble" });
    this.callout = el("div", { class: "callout", hidden: true, role: "status" }, this.calloutText);
    this.controls = el(
      "div",
      { class: "map-controls" },
      el("div", { class: "control-group zoom-group" },
        el("button", { class: "map-btn", type: "button", "aria-label": "Zoom in", onclick: () => this.zoomBy(1.8) }, icon("plus")),
        el("button", { class: "map-btn", type: "button", "aria-label": "Zoom out", onclick: () => this.zoomBy(1 / 1.8) }, icon("minus")),
      ),
      el("div", { class: "control-group" },
        el("button", { class: "map-btn", type: "button", "aria-label": "Fit map", onclick: () => this.fit(true) }, icon("fit")),
      ),
      (this.labelsBtn = el(
        "button",
        {
          class: "map-btn solo",
          type: "button",
          "aria-label": "Names",
          "aria-pressed": String(this.showLabels),
          onclick: () => this.setShowLabels(!this.showLabels, true),
        },
        icon("text"),
      )),
    );
    this.root.append(this.svg, this.callout, this.controls);
  }

  sectorRect(name) {
    const c = name.charCodeAt(0) - 65;
    const r = Number(name.slice(1)) - 1;
    return { x0: c * this.S, y0: r * this.S, x1: (c + 1) * this.S, y1: (r + 1) * this.S };
  }

  worldRect(cls, name) {
    const r = this.sectorRect(name);
    return svg("rect", {
      class: cls,
      x: r.x0,
      y: this.H - r.y1,
      width: r.x1 - r.x0,
      height: r.y1 - r.y0,
      "vector-effect": "non-scaling-stroke",
    });
  }

  // ---------- view ----------

  resize() {
    const box = this.root.getBoundingClientRect();
    const w = Math.max(1, box.width);
    const h = Math.max(1, box.height);
    const old = { ...this.size, midX: this.midX, midY: this.midY };
    const oldFit = this.kFit;
    this.size = { w, h };
    // Floating glass chrome (header, side panel) covers part of the map: centre in what is left.
    const css = getComputedStyle(this.root);
    const px = (name) => parseFloat(css.getPropertyValue(name)) || 0;
    this.inset = px("--map-inset-bottom");
    this.insetTop = Math.min(px("--map-inset-top"), h * 0.4);
    this.insetRight = Math.min(px("--map-inset-right"), w * 0.6);
    this.midX = (w - this.insetRight) / 2;
    this.midY = this.insetTop + (h - this.inset - this.insetTop) / 2;
    this.svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    this.kFit = Math.min(
      (w - this.insetRight - 2 * PAD) / this.W,
      (h - this.inset - this.insetTop - 2 * PAD) / this.H,
    );
    this.overlaysDirty = true;
    if (!old.w) {
      this.fit(false);
      return;
    }
    const c = this.toWorld(old.midX ?? old.w / 2, old.midY);
    const k = (this.view.k / oldFit) * this.kFit;
    this.setView(this.viewCentered(c.x, c.y, k));
  }

  viewCentered(x, y, k) {
    return { k, tx: this.midX - x * k, ty: this.midY - (this.H - y) * k };
  }

  clampView(v) {
    // The centre may roam the whole forest once zoomed in; near "fit" it eases back to the middle.
    const k = clamp(v.k, this.kFit * 0.85, this.kFit * MAX_ZOOM);
    const cx = (this.midX - v.tx) / v.k;
    const cy = this.H - (this.midY - v.ty) / v.k;
    const f = clamp((k / this.kFit - 1) / 0.6, 0, 1);
    const x = clamp(cx, (this.W / 2) * (1 - f), this.W - (this.W / 2) * (1 - f));
    const y = clamp(cy, (this.H / 2) * (1 - f), this.H - (this.H / 2) * (1 - f));
    if (k === v.k && x === cx && y === cy) return v;
    return this.viewCentered(x, y, k);
  }

  setView(v) {
    this.view = this.clampView(v);
    this.render();
  }

  fit(animate) {
    const target = this.viewCentered(this.W / 2, this.H / 2, this.kFit);
    if (animate) this.flyTo(target);
    else this.setView(target);
  }

  flyTo(target, dur = 520) {
    if (reducedMotion()) return this.setView(target);
    this.anim = { type: "fly", from: { ...this.view }, to: this.clampView(target), t0: performance.now(), dur };
    this.requestFrame();
  }

  focusSector(name) {
    const r = this.sectorRect(name);
    const k = Math.max(this.view.k, this.kFit * 1.7);
    this.flyTo(this.viewCentered((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, k));
  }

  zoomBy(factor, ax = this.midX, ay = this.midY, animate = true) {
    if (!animate || reducedMotion()) {
      const k = clamp(this.view.k * factor, this.kFit * 0.85, this.kFit * MAX_ZOOM);
      const w = this.toWorld(ax, ay);
      this.setView({ k, tx: ax - w.x * k, ty: ay - (this.H - w.y) * k });
      return;
    }
    const a = this.anim && this.anim.type === "zoom" ? this.anim : null;
    const k1 = clamp((a ? a.k1 : this.view.k) * factor, this.kFit * 0.85, this.kFit * MAX_ZOOM);
    this.anim = {
      type: "zoom",
      k0: this.view.k,
      k1,
      ax,
      ay,
      w: this.toWorld(ax, ay),
      t0: performance.now(),
      dur: 220,
    };
    this.requestFrame();
  }

  requestFrame() {
    if (!this.raf) this.raf = requestAnimationFrame((t) => this.frame(t));
  }

  frame(now) {
    this.raf = 0;
    let more = false;
    const a = this.anim;
    if (a) {
      const p = clamp((now - a.t0) / a.dur, 0, 1);
      const e = ease(p);
      if (a.type === "fly") {
        const k = a.from.k * (a.to.k / a.from.k) ** e;
        const c0 = { x: (this.midX - a.from.tx) / a.from.k, y: (this.midY - a.from.ty) / a.from.k };
        const c1 = { x: (this.midX - a.to.tx) / a.to.k, y: (this.midY - a.to.ty) / a.to.k };
        const cx = c0.x + (c1.x - c0.x) * e;
        const cy = c0.y + (c1.y - c0.y) * e;
        this.view = { k, tx: this.midX - cx * k, ty: this.midY - cy * k };
      } else if (a.type === "zoom") {
        const k = a.k0 * (a.k1 / a.k0) ** (1 - (1 - p) ** 3);
        this.view = this.clampView({ k, tx: a.ax - a.w.x * k, ty: a.ay - (this.H - a.w.y) * k });
      } else if (a.type === "inertia") {
        const dt = now - a.last;
        a.last = now;
        const decay = Math.exp(-dt / 325);
        a.vx *= decay;
        a.vy *= decay;
        this.view = this.clampView({ ...this.view, tx: this.view.tx + a.vx * dt, ty: this.view.ty + a.vy * dt });
        if (Math.hypot(a.vx, a.vy) < 0.02) this.anim = null;
      }
      if (a.type !== "inertia" && p >= 1) this.anim = null;
      more = !!this.anim;
    }
    if (this.droneMoving(now)) more = true;
    this.render(now);
    if (more) this.requestFrame();
  }

  // ---------- input ----------

  bind() {
    const s = this.svg;
    s.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    s.addEventListener("pointerdown", (e) => this.onDown(e));
    s.addEventListener("pointermove", (e) => this.onMove(e));
    s.addEventListener("pointerup", (e) => this.onUp(e));
    s.addEventListener("pointercancel", (e) => this.onUp(e, true));
    s.addEventListener("pointerleave", (e) => {
      if (e.pointerType === "mouse") this.setHover(null);
    });
    s.addEventListener("dblclick", (e) => {
      const p = this.local(e);
      this.zoomBy(e.shiftKey ? 0.5 : 2, p.x, p.y);
    });
    s.addEventListener("keydown", (e) => this.onKey(e));
  }

  local(e) {
    const r = this.svg.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  onWheel(e) {
    e.preventDefault();
    this.anim = this.anim && this.anim.type === "zoom" ? this.anim : null;
    const p = this.local(e);
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
    const dx = e.deltaX * unit;
    const dy = e.deltaY * unit;
    if (e.ctrlKey) {
      // Trackpad pinch arrives as ctrl+wheel with small deltas: follow the fingers directly.
      this.zoomBy(Math.exp(-dy * 0.012), p.x, p.y, false);
    } else if (e.deltaMode !== 0 || (Math.abs(dy) >= 50 && dx === 0)) {
      // Mouse wheel notch: an eased zoom step.
      this.zoomBy(Math.exp(-dy * 0.0022), p.x, p.y, true);
    } else {
      // Two-finger trackpad scroll pans, like a native map.
      this.setView({ ...this.view, tx: this.view.tx - dx, ty: this.view.ty - dy });
    }
  }

  onDown(e) {
    if (e.button !== undefined && e.button !== 0) return;
    this.svg.setPointerCapture(e.pointerId);
    this.pointerTouch = e.pointerType !== "mouse";
    const p = this.local(e);
    this.pointers.set(e.pointerId, p);
    this.anim = null;
    if (this.pointers.size === 1) {
      this.drag = { x: p.x, y: p.y, tx: this.view.tx, ty: this.view.ty, moved: false, samples: [] };
    } else if (this.pointers.size === 2) {
      const [a, b] = [...this.pointers.values()];
      this.pinch = {
        dist: Math.hypot(b.x - a.x, b.y - a.y) || 1,
        mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
        view: { ...this.view },
      };
      if (this.drag) this.drag.moved = true;
    }
  }

  onMove(e) {
    const p = this.local(e);
    if (!this.pointers.has(e.pointerId)) {
      if (e.pointerType === "mouse") this.setHover(this.hit(p.x, p.y));
      return;
    }
    this.pointers.set(e.pointerId, p);
    if (this.pointers.size >= 2 && this.pinch) {
      const [a, b] = [...this.pointers.values()];
      const dist = Math.hypot(b.x - a.x, b.y - a.y) || 1;
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const v0 = this.pinch.view;
      const k = clamp((v0.k * dist) / this.pinch.dist, this.kFit * 0.85, this.kFit * MAX_ZOOM);
      const wx = (this.pinch.mid.x - v0.tx) / v0.k;
      const wy = (this.pinch.mid.y - v0.ty) / v0.k;
      this.setView({ k, tx: mid.x - wx * k, ty: mid.y - wy * k });
      return;
    }
    const d = this.drag;
    if (!d) return;
    const dx = p.x - d.x;
    const dy = p.y - d.y;
    if (!d.moved && Math.hypot(dx, dy) > 5) {
      d.moved = true;
      this.svg.classList.add("dragging");
    }
    if (d.moved) {
      this.setView({ ...this.view, tx: d.tx + dx, ty: d.ty + dy });
      d.samples.push({ t: performance.now(), x: p.x, y: p.y });
      if (d.samples.length > 6) d.samples.shift();
    }
  }

  onUp(e, cancelled = false) {
    if (!this.pointers.has(e.pointerId)) return;
    const p = this.pointers.get(e.pointerId);
    this.pointers.delete(e.pointerId);
    this.svg.classList.remove("dragging");
    if (this.pointers.size === 1) {
      // Pinch ended with one finger still down: continue as a drag from here.
      const [rest] = [...this.pointers.values()];
      this.pinch = null;
      this.drag = { x: rest.x, y: rest.y, tx: this.view.tx, ty: this.view.ty, moved: true, samples: [] };
      return;
    }
    const d = this.drag;
    this.drag = null;
    this.pinch = null;
    if (!d || cancelled) return;
    if (!d.moved) {
      this.tap(p);
      return;
    }
    const s = d.samples;
    if (s.length >= 2 && !reducedMotion()) {
      const a = s[0];
      const b = s[s.length - 1];
      const dt = b.t - a.t;
      if (dt > 0 && performance.now() - b.t < 80) {
        this.anim = { type: "inertia", vx: (b.x - a.x) / dt, vy: (b.y - a.y) / dt, last: performance.now() };
        this.requestFrame();
      }
    }
  }

  tap(p) {
    const now = performance.now();
    const last = this.lastTap;
    this.lastTap = { t: now, x: p.x, y: p.y };
    if (last && now - last.t < 300 && Math.hypot(p.x - last.x, p.y - last.y) < 24 && this.pointerTouch) {
      this.zoomBy(2, p.x, p.y);
      return;
    }
    const hit = this.hit(p.x, p.y);
    this.select(hit !== null && hit === this.selected ? null : hit);
  }

  onKey(e) {
    const step = 80;
    const keys = {
      "+": () => this.zoomBy(1.5),
      "=": () => this.zoomBy(1.5),
      "-": () => this.zoomBy(1 / 1.5),
      0: () => this.fit(true),
      ArrowLeft: () => this.flyTo({ ...this.view, tx: this.view.tx + step }, 200),
      ArrowRight: () => this.flyTo({ ...this.view, tx: this.view.tx - step }, 200),
      ArrowUp: () => this.flyTo({ ...this.view, ty: this.view.ty + step }, 200),
      ArrowDown: () => this.flyTo({ ...this.view, ty: this.view.ty - step }, 200),
      Escape: () => this.select(null),
    };
    if (keys[e.key]) {
      e.preventDefault();
      keys[e.key]();
    }
  }

  hit(px, py) {
    let best = null;
    let bestD = HIT_PX;
    for (const n of this.world.nodes) {
      const d = Math.hypot(this.sx(n.x_m) - px, this.sy(n.y_m) - py);
      if (d < bestD) {
        best = n.id;
        bestD = d;
      }
    }
    return best;
  }

  setHover(id) {
    clearTimeout(this.hoverTimer);
    if (id === this.hover) return;
    this.svg.classList.toggle("over-node", id !== null);
    this.hoverTimer = setTimeout(
      () => {
        this.hover = id;
        this.renderNodes();
      },
      id === null ? 60 : HOVER_DELAY_MS,
    );
  }

  select(id) {
    this.selected = id;
    this.renderNodes();
    this.onSelectNode(id === null ? null : this.world.nodes.find((n) => n.id === id));
  }

  setShowLabels(on, fromUser = false) {
    this.showLabels = !!on;
    this.labelsBtn.setAttribute("aria-pressed", String(this.showLabels));
    this.renderNodes();
    if (fromUser) this.onLabelsToggle(this.showLabels);
  }

  // ---------- state from the station ----------

  flashNode(id, kind) {
    const ms = kind === "red" ? RED_MS : AMBER_MS;
    const cur = this.nodeState.get(id);
    if (cur && cur.state === "red" && kind === "amber" && cur.until > performance.now()) return;
    this.nodeState.set(id, { state: kind, until: performance.now() + ms });
    this.renderNodes();
  }

  clearNodes() {
    this.nodeState.clear();
    this.renderNodes();
  }

  expireStates() {
    const now = performance.now();
    let changed = false;
    for (const [id, s] of this.nodeState) {
      if (s.until <= now) {
        this.nodeState.delete(id);
        changed = true;
      }
    }
    if (changed) this.renderNodes();
  }

  setRain(sector, rain) {
    let r = this.rainRects.get(sector);
    if (!r && rain) {
      r = this.worldRect("rain-cell", sector);
      this.rainRects.set(sector, r);
      this.gRain.append(r);
      requestAnimationFrame(() => r.classList.add("on"));
      return;
    }
    if (r) r.classList.toggle("on", !!rain);
  }

  clearRain() {
    for (const r of this.rainRects.values()) r.classList.remove("on");
  }

  setIncidents(list) {
    const keep = new Set();
    for (const inc of list) {
      if (!inc.sector || !/^[A-Z]\d{1,2}$/.test(inc.sector)) continue;
      keep.add(inc.sector);
      let r = this.incidentRects.get(inc.sector);
      if (!r) {
        r = this.worldRect("incident-cell", inc.sector);
        this.incidentRects.set(inc.sector, r);
        this.gIncidents.append(r);
      }
      r.classList.toggle("selected", !!inc.selected);
      r.classList.toggle("urgent", !!inc.urgent);
    }
    for (const [sector, r] of this.incidentRects) {
      if (!keep.has(sector)) {
        r.remove();
        this.incidentRects.delete(sector);
      }
    }
  }

  setDrone(msg) {
    const now = performance.now();
    const to = { x: msg.x_m, y: msg.y_m };
    const cur = this.dronePos(now) || to;
    const gap = this.lastDroneMsg ? now - this.lastDroneMsg : 250;
    this.lastDroneMsg = now;
    const d = this.drone;
    const pathKey = JSON.stringify(msg.path || null);
    if (msg.flying && pathKey !== d.pathKey) d.trail = [];
    d.pathKey = pathKey;
    const teleport = Math.hypot(cur.x - to.x, cur.y - to.y) > 800;
    d.from = teleport ? to : cur;
    d.to = to;
    d.t0 = now;
    d.dur = msg.flying && !teleport ? clamp(gap, 120, 1000) : 0;
    d.flying = !!msg.flying;
    d.path = msg.path || [];
    d.target = msg.target || null;
    d.phase = msg.phase;
    if (d.flying) d.trail.push(to);
    else d.trail = [];
    const text = d.flying && msg.observation ? msg.observation : "";
    if (text !== this.calloutText.textContent) {
      this.calloutText.textContent = text;
      this.calloutSize = null;
    }
    this.callout.hidden = !text;
    this.requestFrame();
  }

  dronePos(now) {
    const d = this.drone;
    if (!d.to) return null;
    const p = d.dur ? clamp((now - d.t0) / d.dur, 0, 1) : 1;
    return { x: d.from.x + (d.to.x - d.from.x) * p, y: d.from.y + (d.to.y - d.from.y) * p };
  }

  droneMoving(now) {
    const d = this.drone;
    return d.dur > 0 && now - d.t0 < d.dur;
  }

  // ---------- render ----------

  render(now = performance.now()) {
    const { k, tx, ty } = this.view;
    const { w, h } = this.size;
    this.gWorld.setAttribute("transform", `matrix(${k} 0 0 ${k} ${tx} ${ty})`);
    this.hatch.setAttribute("patternTransform", `translate(${tx} ${ty}) rotate(45)`);

    const sectorPx = this.S * k;
    for (const z of this.noFly) {
      const x = this.sx(z.rect.x0);
      const y = this.sy(z.rect.y1);
      const size = { x, y, width: (z.rect.x1 - z.rect.x0) * k, height: (z.rect.y1 - z.rect.y0) * k };
      for (const r of [z.fill, z.edge]) for (const [a, v] of Object.entries(size)) r.setAttribute(a, v);
      z.label.setAttribute("x", x + size.width / 2);
      z.label.setAttribute("y", y + size.height / 2 + 4);
      z.label.classList.toggle("shown", sectorPx >= 110);
    }

    const bottom = Math.min(this.sy(0) + 15, h - this.inset - 7);
    this.colLabels.forEach((t, c) => {
      const x = this.sx((c + 0.5) * this.S);
      t.setAttribute("x", x);
      t.setAttribute("y", bottom);
      t.classList.toggle("off", x < 12 || x > w - 12);
    });
    const left = Math.max(this.sx(0) - 11, 10);
    this.rowLabels.forEach((t, r) => {
      const y = this.sy((r + 0.5) * this.S) + 4;
      t.setAttribute("x", left);
      t.setAttribute("y", y);
      t.classList.toggle("off", y < 12 || y > h - this.inset - 20);
    });

    const showSectors = sectorPx >= SECTOR_LABELS_AT_PX;
    this.gSectorLabels.classList.toggle("shown", showSectors);
    if (showSectors) {
      for (const s of this.sectorLabels) {
        s.t.setAttribute("x", this.sx(s.c * this.S) + 7);
        s.t.setAttribute("y", this.sy((s.r + 1) * this.S) + 15);
      }
    }

    this.base.setAttribute(
      "transform",
      `translate(${this.sx(this.world.base.x_m)} ${this.sy(this.world.base.y_m)})`,
    );
    for (const n of this.nodes) {
      n.g.setAttribute("transform", `translate(${this.sx(n.node.x_m)} ${this.sy(n.node.y_m)})`);
    }
    this.renderNodes();
    this.renderDrone(now);
  }

  renderDrone(now) {
    const d = this.drone;
    const pos = this.dronePos(now);
    const atBase = !d.flying;
    this.droneG.classList.toggle("flying", d.flying);
    this.droneG.classList.toggle("parked", atBase);
    if (pos) {
      this.droneG.setAttribute("transform", `translate(${this.sx(pos.x)} ${this.sy(pos.y)})`);
    }
    const pts = (list) => list.map((p) => `${this.sx(p[0] ?? p.x)},${this.sy(p[1] ?? p.y)}`).join(" ");
    this.planLine.setAttribute("points", d.flying ? pts(d.path) : "");
    this.trailLine.setAttribute("points", d.flying && pos ? pts([d.path[0] || pos, ...d.trail.slice(0, -1), pos]) : "");
    if (!this.callout.hidden && d.target) {
      const x = this.sx(d.target[0]);
      const y = this.sy(d.target[1]);
      this.callout.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
    }
  }

  invalidateOverlays() {
    this.overlaysDirty = true;
    this.renderNodes();
  }

  overlayBoxes() {
    if (this.overlaysDirty || !this.overlayCache) {
      const base = this.root.getBoundingClientRect();
      const shown = (e) => !e.hidden && e.getClientRects().length > 0;
      const parts = [...this.root.querySelectorAll(".control-group, .map-btn.solo, .drone-chip, .node-card")];
      this.overlayCache = parts.filter(shown).map((e) => {
        const r = e.getBoundingClientRect();
        return {
          x0: r.left - base.left - 4,
          x1: r.right - base.left + 4,
          y0: r.top - base.top - 4,
          y1: r.bottom - base.top + 4,
        };
      });
      this.overlaysDirty = false;
    }
    return this.overlayCache;
  }

  markerBoxes(now) {
    const box = (x, y, r) => ({ x0: x - r, x1: x + r, y0: y - r, y1: y + r });
    const boxes = this.world.nodes.map((n) => box(this.sx(n.x_m), this.sy(n.y_m), 8));
    const d = this.drone;
    const p = d.flying ? this.dronePos(now) : null;
    if (p) boxes.push(box(this.sx(p.x), this.sy(p.y), 12));
    if (!this.callout.hidden && d.target) {
      if (!this.calloutSize) {
        this.calloutSize = { w: this.calloutText.offsetWidth, h: this.calloutText.offsetHeight };
      }
      const x = this.sx(d.target[0]);
      const y = this.sy(d.target[1]);
      const { w, h } = this.calloutSize;
      boxes.push({ x0: x - w / 2 - 4, x1: x + w / 2 + 4, y0: y - 18 - h, y1: y - 10 });
    }
    return boxes;
  }

  renderNodes() {
    const now = performance.now();
    const allNames = this.showLabels || this.S * this.view.k >= ALL_NAMES_AT_SECTOR_PX;
    const candidates = [];
    for (const n of this.nodes) {
      const st = this.nodeState.get(n.node.id);
      const state = st && st.until > now ? st.state : "idle";
      const g = n.g;
      g.classList.toggle("idle", state === "idle");
      g.classList.toggle("amber", state === "amber");
      g.classList.toggle("red", state === "red");
      g.classList.toggle("hover", this.hover === n.node.id);
      g.classList.toggle("selected", this.selected === n.node.id);
      const priority =
        (this.selected === n.node.id ? 8 : 0) +
        (this.hover === n.node.id ? 4 : 0) +
        (state === "red" ? 2 : state === "amber" ? 1 : 0);
      if (allNames || priority > 0) candidates.push({ n, priority });
      else n.label.classList.remove("shown");
    }
    // Place the most important names first; drop names that would collide with each other,
    // with node dots, the drone, its callout, or the map's floating controls.
    candidates.sort((a, b) => b.priority - a.priority);
    const placed = [...this.overlayBoxes(), ...this.markerBoxes(now)];
    const { w, h } = this.size;
    const bottomEdge = h - this.inset - 2;
    for (const { n } of candidates) {
      if (!this.labelWidth.has(n.node.id)) {
        const len = n.label.getComputedTextLength();
        if (len > 0) this.labelWidth.set(n.node.id, len);
      }
      const lw = this.labelWidth.get(n.node.id) || n.node.name.length * 6.6;
      const x = this.sx(n.node.x_m);
      const y = this.sy(n.node.y_m);
      // Right of the dot first, then left, below, above (x, y, anchor are label offsets).
      const spots = [
        { x0: x + 10, x1: x + 14 + lw, y0: y - 9, y1: y + 9, lx: 12, ly: 4, anchor: "start" },
        { x0: x - 14 - lw, x1: x - 10, y0: y - 9, y1: y + 9, lx: -12, ly: 4, anchor: "end" },
        { x0: x - lw / 2 - 2, x1: x + lw / 2 + 2, y0: y + 9, y1: y + 25, lx: 0, ly: 22, anchor: "middle" },
        { x0: x - lw / 2 - 2, x1: x + lw / 2 + 2, y0: y - 25, y1: y - 9, lx: 0, ly: -14, anchor: "middle" },
      ];
      const free = (b) =>
        b.x0 >= 2 &&
        b.x1 <= w - 2 &&
        b.y0 >= 2 &&
        b.y1 <= bottomEdge &&
        !placed.some((p) => b.x0 < p.x1 && b.x1 > p.x0 && b.y0 < p.y1 && b.y1 > p.y0);
      const spot = spots.find(free);
      if (!spot) {
        n.label.classList.remove("shown");
        continue;
      }
      placed.push(spot);
      n.label.setAttribute("x", spot.lx);
      n.label.setAttribute("y", spot.ly);
      n.label.setAttribute("text-anchor", spot.anchor);
      n.label.classList.add("shown");
    }
  }
}
