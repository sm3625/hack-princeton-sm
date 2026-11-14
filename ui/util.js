// Small helpers. All text goes in through textContent or attributes, never innerHTML.

const SVG_NS = "http://www.w3.org/2000/svg";

function build(node, attrs, children) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = String(value);
    else if (key === "class") node.setAttribute("class", value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function el(tag, attrs, ...children) {
  return build(document.createElement(tag), attrs, children);
}

export function svg(tag, attrs, ...children) {
  return build(document.createElementNS(SVG_NS, tag), attrs, children);
}

export function icon(name, cls = "icon") {
  // Icons are fixed path data defined here, never user input.
  const paths = {
    play: "M8 5.5v13l11-6.5z",
    pause: "M7 5h4v14H7zM13 5h4v14h-4z",
    restart: "M1 4v6h6M3.51 15a9 9 0 1 0 2.13-9.36L1 10",
    sliders: "M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4",
    check: "M5 12.5l4.5 4.5L19 7.5",
    cross: "M6 6l12 12M18 6L6 18",
    chevronRight: "M9 5l7 7-7 7",
    chevronLeft: "M15 5l-7 7 7 7",
    plus: "M12 5v14M5 12h14",
    minus: "M5 12h14",
    fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
    text: "M4 7V4h16v3M9 20h6M12 4v16",
    drop: "M12 3.5c3 4 5.5 7 5.5 10a5.5 5.5 0 0 1-11 0c0-3 2.5-6 5.5-10z",
    shield: "M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6z",
    doc: "M7 3h7l4 4v14H7zM14 3v4h4",
  };
  const stroked = !["play", "pause", "drop"].includes(name);
  return svg(
    "svg",
    { class: cls, viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" },
    svg("path", {
      d: paths[name] || "",
      fill: stroked ? "none" : "currentColor",
      stroke: stroked ? "currentColor" : "none",
      "stroke-width": stroked ? 2 : 0,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }),
  );
}

// ---------- per-user preferences (localStorage may be unavailable) ----------

const PREFS_KEY = "fg.prefs";

function readPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

export const prefs = {
  get(key, fallback) {
    const value = readPrefs()[key];
    return value === undefined ? fallback : value;
  },
  set(key, value) {
    try {
      const all = readPrefs();
      all[key] = value;
      localStorage.setItem(PREFS_KEY, JSON.stringify(all));
    } catch {
      /* private mode or storage blocked: keep working without saving */
    }
  },
};

// ---------- HTTP ----------

export class HttpError extends Error {
  constructor(status) {
    super(`HTTP ${status}`);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new HttpError(res.status);
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : null;
}

// ---------- formatting ----------

const SOUNDS = {
  chainsaw: "Chainsaw",
  gunshot: "Gunshot",
  vehicle: "Vehicle",
  axe: "Axe",
  background: "Background",
};

export function soundLabel(sound) {
  return SOUNDS[sound] || titleCase(String(sound || "Sound"));
}

export function titleCase(text) {
  const t = String(text).replace(/[_-]+/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : t;
}

export function percent(confidence) {
  return `${Math.round(Number(confidence) * 100)}%`;
}

export function toSeconds(t) {
  if (typeof t === "number") return t > 1e12 ? t / 1000 : t;
  const parsed = Date.parse(t);
  return Number.isNaN(parsed) ? null : parsed / 1000;
}

export function relTime(now, t) {
  const secs = toSeconds(t);
  if (secs === null || secs === undefined) return "";
  const d = Math.max(0, now - secs);
  if (d < 60) return "now";
  if (d < 3600) return `${Math.floor(d / 60)} min`;
  if (d < 86400) return `${Math.floor(d / 3600)} h`;
  return `${Math.floor(d / 86400)} d`;
}

export function clockText(t) {
  const d = new Date(t * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function fullTime(t) {
  const secs = toSeconds(t);
  if (secs === null) return "";
  return new Date(secs * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export const reducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
