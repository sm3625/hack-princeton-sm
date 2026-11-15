// Story: one plain-language caption over the map for what just happened, and a run summary when
// the scenario's events are over. It only listens (app.js re-dispatches bus messages as "fg-bus").
import { api, el, percent, soundLabel } from "./util.js";

const box = document.getElementById("story");
const MIN_SHOW_MS = 2600;
const QUIET = new Set(["dismissed", "watching"]);
const ASKED = new Set(["awaiting_approval", "dispatched"]);

let run = fresh();
let queue = [];
let showing = false;
let timer = null;

function fresh() {
  return { id: null, scenario: null, complete: false, flying: false, summaryOff: false,
    detections: new Set(), incidents: new Map(), asked: new Set() };
}

function reset(keepId) {
  clearTimeout(timer);
  run = fresh();
  run.id = keepId ?? null;
  queue = [];
  showing = false;
  box.hidden = true;
  box.replaceChildren();
}

// ---------- captions ----------

function say(who, text, tone = "", priority = 1) {
  if (!run.scenario) return;
  // Sensor lines are filler: never let them delay what the agent or a ranger did.
  if (priority === 0 && (queue.length || showing)) return;
  queue = queue.filter((c) => c.priority > 0).slice(-3);
  queue.push({ who, text, tone, priority });
  if (!showing) next();
}

function next() {
  clearTimeout(timer);
  const c = queue.shift();
  if (!c) {
    showing = false;
    if (run.complete) summary();
    return;
  }
  showing = true;
  box.hidden = false;
  box.replaceChildren(
    el("div", { class: `story-line ${c.tone}` }, el("span", { class: "story-who" }, c.who), el("span", { class: "story-text" }, c.text)));
  timer = setTimeout(next, MIN_SHOW_MS);
}

const short = (text, max = 90) => {
  const t = String(text || "").trim();
  return t && t.length <= max ? t : "";
};

function onIncident(inc) {
  if (!inc || inc.id === undefined) return;
  const id = String(inc.id);
  const before = run.incidents.get(id);
  run.incidents.set(id, { ...before, ...inc });
  const title = inc.title || before?.title || "incident";
  const sector = inc.sector || before?.sector || "";
  if (!before) say("Agent", `Investigating: ${title}`);
  if (ASKED.has(inc.status) || inc.ranger_asked) run.asked.add(id);
  if (!inc.status || before?.status === inc.status) return run.complete && summary();
  if (inc.status === "dismissed") say("Agent", short(inc.summary) || `Dismissed: ${title}. No one woken.`, "quiet", 2);
  else if (inc.status === "watching") say("Agent", `Watching ${title}. Not enough to wake anyone.`, "quiet");
  else if (inc.status === "awaiting_approval") say("Agent", `Wants rangers sent to ${sector}. Waiting for a person.`, "ask", 3);
  else if (inc.status === "dispatched") say("Ranger", `Approved. Team on the way to ${sector}.`, "go", 3);
  else if (inc.status === "closed") say("Agent", `Case file written: ${title}`, "", 2);
  if (run.complete) summary();
}

function onDetection(m) {
  const d = m.detection || {};
  if (d.verified !== true || d.seq === undefined) return;
  const key = `${d.node_id}:${d.seq}`;
  if (run.detections.has(key)) return;
  run.detections.add(key);
  const node = m.node || "A sensor";
  say("Sensor", `${node} heard ${soundLabel(d.sound).toLowerCase()} · ${percent(d.confidence)} · signed`, "", 0);
}

function onClock(m) {
  if (m.run_id && run.id && m.run_id !== run.id) reset(m.run_id);
  if (m.run_id) run.id = m.run_id;
  run.scenario = m.scenario || null;
  if (!run.scenario) return;
  if (m.events_complete && !run.complete) {
    run.complete = true;
    if (!showing) summary();
  }
}

// ---------- run summary ----------

async function summary() {
  if (!run.complete || run.summaryOff || showing) return;
  const id = run.id;
  // A page opened mid-run missed earlier messages: count from the station, not from memory.
  try {
    const [det, inc] = await Promise.all([api("/api/detections?limit=500"), api("/api/incidents")]);
    if (id !== run.id) return;
    for (const d of det?.detections || []) if (d.verified === true) run.detections.add(`${d.node_id}:${d.seq}`);
    for (const i of (Array.isArray(inc) ? inc : inc?.incidents) || []) {
      run.incidents.set(String(i.id), { ...run.incidents.get(String(i.id)), ...i });
      if (ASKED.has(i.status) || i.ranger_asked) run.asked.add(String(i.id));
    }
  } catch {
    /* fall back to what this page saw */
  }
  if (showing || !run.complete || run.summaryOff) return;
  const all = [...run.incidents.values()];
  const alerts = run.detections.size;
  if (!alerts && !all.length) return;
  const asked = run.asked.size;
  const quiet = all.filter((i) => QUIET.has(i.status) && !run.asked.has(String(i.id))).length;
  const working = all.some((i) => i.status === "open");
  const stat = (n, label, tone = "") => el("div", { class: `story-stat ${tone}` }, el("strong", {}, String(n)), el("span", {}, label));
  const close = el("button", { class: "story-close", type: "button", "aria-label": "Close summary" }, "×");
  close.addEventListener("click", () => {
    run.summaryOff = true;
    box.hidden = true;
  });
  box.hidden = false;
  box.replaceChildren(
    el("div", { class: "story-summary" },
      close,
      el("div", { class: "story-kicker" }, working ? "This run so far" : "This run"),
      el("div", { class: "story-stats" },
        stat(alerts, alerts === 1 ? "sensor alert" : "sensor alerts"),
        el("span", { class: "story-arrow", "aria-hidden": "true" }, "→"),
        stat(all.length, all.length === 1 ? "incident" : "incidents"),
        el("span", { class: "story-arrow", "aria-hidden": "true" }, "→"),
        stat(asked, asked === 1 ? "ranger request" : "ranger requests", "ask")),
      el("p", { class: "story-note" },
        `${quiet} handled without waking anyone. Every alert signature-checked.`)));
}

window.addEventListener("fg-bus", (e) => {
  const m = e.detail;
  switch (m && m.type) {
    case "clock":
      return onClock(m);
    case "scenario":
    case "run_reset":
      return reset(m.run_id);
    case "detection":
      return onDetection(m);
    case "rejected":
      return m.reason === "bad_signature" ? say("Station", "Altered record rejected. Signature did not match.", "ask", 2) : undefined;
    case "incident":
      return onIncident(m.incident);
    case "heads_up":
      return say("Agent", `Early heads-up: two dry sensors agree in ${m.sector}.`, "ask", 2);
    case "drone": {
      const flying = !!m.flying;
      if (flying && !run.flying) say("Drone", "Launched to take a look.", "", 2);
      run.flying = flying;
      return undefined;
    }
    default:
      return undefined;
  }
});
