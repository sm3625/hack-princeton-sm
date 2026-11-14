// Real OpenStreetMap geography: static files made by scripts/fetch_osm.py (c) OpenStreetMap contributors, ODbL.
// pilot_block.json: features in local metres (x east, y north of the world origin).
// reserve.json: simplified reserve and park outlines in lon/lat for the location inset.

import { svg } from "./util.js";

export async function loadGeo() {
  const get = (name) =>
    fetch(`/ui/geo/${name}`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  const [block, reserve] = await Promise.all([get("pilot_block.json"), get("reserve.json")]);
  return { block, reserve };
}

// The scaled map group has y pointing down, so world y (north) is flipped with H.
const ring = (pts, H) => `M${pts.map((p) => `${p[0]} ${H - p[1]}`).join("L")}Z`;
const line = (pts, H) => `M${pts.map((p) => `${p[0]} ${H - p[1]}`).join("L")}`;
const LINE_ORDER = ["canal", "stream", "river", "path", "track", "road"];
const AREA_ORDER = ["farm", "grass", "scrub", "wood", "wetland", "water"];

// Base-map layers in world metres (inside the scaled group; strokes keep a constant pixel width).
export function buildGeoLayer(block, W, H) {
  const g = svg("g", { class: "geo", "aria-hidden": "true" });
  const m = block.margin_m ?? 0;
  const frame = `M${-m} ${-m}H${W + m}V${H + m}H${-m}ZM0 0V${H}H${W}V0Z`;
  const areas = block.features.filter((f) => f.t === "area");
  const lines = block.features.filter((f) => f.t === "line");
  for (const c of AREA_ORDER) {
    const items = areas.filter((f) => f.c === c);
    if (!items.length) continue;
    const d = items.map((f) => f.r.map((r) => ring(r, H)).join("")).join("");
    const attrs = { class: `geo-${c}`, d, "fill-rule": "evenodd" };
    if (c === "wetland") attrs.fill = "url(#wetland-hatch)";
    g.append(svg("path", attrs));
  }
  for (const c of LINE_ORDER) {
    const items = lines.filter((f) => f.c === c);
    if (!items.length) continue;
    const d = items.map((f) => line(f.p, H)).join("");
    g.append(svg("path", { class: `geo-${c}`, d, "vector-effect": "non-scaling-stroke" }));
  }
  // Soften everything outside the pilot block so it reads as surroundings.
  g.append(svg("path", { class: "geo-veil", d: frame, "fill-rule": "evenodd" }));
  // ...and fade the clipped edge of the data into the map background so no hard cut shows.
  const grad = (id, x2, y2) =>
    svg(
      "linearGradient",
      { id, x1: 0, y1: 0, x2, y2 },
      svg("stop", { offset: 0, class: "geo-fade-in" }),
      svg("stop", { offset: 1, class: "geo-fade-out" }),
    );
  g.append(
    svg("defs", {}, grad("geo-fade-l", 1, 0), grad("geo-fade-r", 1, 0), grad("geo-fade-t", 0, 1), grad("geo-fade-b", 0, 1)),
    svg("rect", { class: "geo-fade", x: -m, y: -m, width: m, height: H + 2 * m, fill: "url(#geo-fade-l)", transform: `rotate(180 ${-m / 2} ${H / 2})` }),
    svg("rect", { class: "geo-fade", x: W, y: -m, width: m, height: H + 2 * m, fill: "url(#geo-fade-r)" }),
    svg("rect", { class: "geo-fade", x: -m, y: -m, width: W + 2 * m, height: m, fill: "url(#geo-fade-t)", transform: `rotate(180 ${W / 2} ${-m / 2})` }),
    svg("rect", { class: "geo-fade", x: -m, y: H, width: W + 2 * m, height: m, fill: "url(#geo-fade-b)" }),
  );
  return g;
}

// Name labels sit in the screen-space overlay; this picks a spot and angle for each named line.
export function geoLabels(block, W, H) {
  const inside = (p) => p[0] > 60 && p[0] < W - 60 && p[1] > 60 && p[1] < H - 60;
  const byName = new Map();
  for (const f of block.features) {
    if (f.t !== "line" || !f.n) continue;
    const run = f.p.filter(inside);
    if (run.length < 2) continue;
    const best = byName.get(f.n);
    if (!best || run.length > best.run.length) byName.set(f.n, { f, run });
  }
  const labels = [];
  for (const [name, { f, run }] of byName) {
    const i = Math.floor((run.length - 1) / 2);
    const a = run[i];
    const b = run[i + 1];
    const x = (a[0] + b[0]) / 2;
    const y = (a[1] + b[1]) / 2;
    // World y is north; screen y is down, so the on-screen angle flips sign.
    let deg = (-Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI;
    if (deg > 90) deg -= 180;
    if (deg < -90) deg += 180;
    labels.push({ name, cls: f.c, x, y, deg });
  }
  return labels;
}

// Location inset: reserve and park outlines, Tikal, and the pilot block.
export function renderInset(root, reserve) {
  if (!reserve?.reserve?.rings?.length) return false;
  const rings = reserve.reserve.rings;
  const lat0 = reserve.pilot.lat;
  const kx = Math.cos((lat0 * Math.PI) / 180);
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const r of rings) {
    for (const [lon, lat] of r) {
      minX = Math.min(minX, lon * kx);
      maxX = Math.max(maxX, lon * kx);
      minY = Math.min(minY, lat);
      maxY = Math.max(maxY, lat);
    }
  }
  const VW = 246;
  const pad = 4;
  const s = (VW - pad * 2) / (maxX - minX);
  const VH = Math.ceil((maxY - minY) * s + pad * 2 + 6);
  const px = (lon) => pad + (lon * kx - minX) * s;
  const py = (lat) => pad + 6 + (maxY - lat) * s;
  const path = (rs) => rs.map((r) => `M${r.map(([lon, lat]) => `${px(lon).toFixed(1)} ${py(lat).toFixed(1)}`).join("L")}Z`).join("");

  root.setAttribute("viewBox", `0 0 ${VW} ${VH}`);
  root.replaceChildren(
    svg("text", { class: "ri-country", x: VW * 0.5, y: 5, text: "MEXICO" }),
    svg("path", { class: "ri-reserve", d: path(rings) }),
    ...(reserve.park?.rings?.length ? [svg("path", { class: "ri-park", d: path(reserve.park.rings) })] : []),
  );
  const parkRing = reserve.park?.rings?.[0];
  if (parkRing) {
    const xs = parkRing.map((p) => px(p[0]));
    const ys = parkRing.map((p) => py(p[1]));
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const top = Math.min(...ys);
    root.append(svg("text", { class: "ri-zone", x: cx - 8, y: top + 15, "text-anchor": "middle", text: "Laguna del Tigre NP" }));
  }
  for (const p of reserve.places || []) {
    root.append(
      svg("circle", { class: "ri-site", cx: px(p.lon), cy: py(p.lat), r: 2 }),
      svg("text", { class: "ri-site-label", x: px(p.lon) + 4, y: py(p.lat) + 3, text: p.n }),
    );
  }
  const cx = px(reserve.pilot.lon);
  const cy = py(reserve.pilot.lat);
  root.append(
    svg("circle", { class: "ri-pilot-halo", cx, cy, r: 6 }),
    svg("rect", { class: "ri-pilot", x: cx - 2.5, y: cy - 2.5, width: 5, height: 5, rx: 1 }),
    svg("text", { class: "ri-pilot-label", x: cx, y: cy + 14, text: "Pilot" }),
  );
  return true;
}
