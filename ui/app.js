// Ranger station UI. Message shapes: common/contracts.py. Text is always set with
// textContent; the only HTML inserted is the server-sanitized case file / plan "html" field.

import { ForestMap } from "./map.js";
import {
  HttpError,
  api,
  clockText,
  el,
  fullTime,
  icon,
  percent,
  prefs,
  relTime,
  soundLabel,
  titleCase,
  toSeconds,
} from "./util.js";

const $ = (id) => document.getElementById(id);
const FEED_MAX = 200;
const STEPS_MAX = 100;
const AGENTS = { commander: "Commander", triage: "Triage", "drone-pilot": "Drone", evidence: "Evidence" };
const REASONS = { bad_signature: "Bad signature", replay: "Replay", oversize: "Oversize" };
const STATUSES = {
  open: "Open",
  new: "New",
  triage: "Triage",
  triaging: "Triage",
  investigating: "Checking",
  watching: "Watching",
  watch: "Watching",
  awaiting_approval: "Awaiting approval",
  pending_approval: "Awaiting approval",
  approval: "Awaiting approval",
  dispatched: "Dispatched",
  rejected: "Not dispatched",
  dismissed: "Dismissed",
  false_alarm: "Dismissed",
  closed: "Closed",
  resolved: "Closed",
};
const DECIDED = { approve: "Approved", edit: "Sent", reject: "Rejected" };

const state = {
  world: null,
  nodesById: new Map(),
  nodesByName: new Map(),
  sim: { sim_time: Date.now() / 1000, speed: 1, paused: false, running: false, scenario: null, feed: "stub", at: performance.now() },
  scenarios: [],
  feed: [],
  unseenFeed: 0,
  incidents: new Map(),
  approvals: new Map(),
  selected: null,
  tab: prefs.get("tab", "incidents") === "feed" ? "feed" : "incidents", // "results" only once it has data
  results: null,
  rain: new Set(),
  drone: null,
  lastByNode: new Map(),
  seenDetections: new Set(), // "node:seq:verified": a refresh or reconnect must not duplicate rows
  selectedNode: null,
  map: null,
};

// ---------- time ----------

function simNow() {
  const s = state.sim;
  if (s.paused) return s.sim_time;
  return s.sim_time + ((performance.now() - s.at) / 1000) * s.speed;
}

function timeEl(t) {
  const secs = toSeconds(t);
  return el("time", { class: "rel", dataset: { t: secs ?? "" }, title: fullTime(secs) }, relTime(simNow(), secs));
}

function refreshTimes() {
  const now = simNow();
  for (const t of document.querySelectorAll("time.rel")) {
    const text = relTime(now, Number(t.dataset.t));
    if (t.textContent !== text) t.textContent = text;
  }
  $("clock-time").textContent = clockText(now);
}

// ---------- small UI pieces ----------

let toastTimer = 0;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  t.classList.add("shown");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    t.classList.remove("shown");
    setTimeout(() => (t.hidden = true), 250);
  }, 2200);
}

function nodeName(id) {
  const n = state.nodesById.get(Number(id));
  return n ? n.name : "Unknown node";
}

function statusLabel(status) {
  if (!status) return "Open";
  return STATUSES[String(status).toLowerCase()] || titleCase(status);
}

function isClosed(status) {
  return /closed|dismiss|resolved|false_alarm|rejected/i.test(String(status || ""));
}

function hasPendingApproval(incidentId) {
  for (const a of state.approvals.values()) if (a.incidentId === incidentId && a.phase !== "done") return true;
  return false;
}

function statusClass(inc) {
  if (hasPendingApproval(inc.id) || /approv/i.test(inc.status || "")) return "s-urgent";
  if (/dispatch/i.test(inc.status || "")) return "s-active";
  if (isClosed(inc.status)) return "s-closed";
  return "s-open";
}

function actionText(action) {
  if (!action) return "";
  if (typeof action === "string") return titleCase(action);
  return titleCase(action.name || action.tool || action.action || "");
}

function actionNote(action) {
  if (!action || typeof action !== "object") return "";
  const args = action.args || action.arguments || {};
  return typeof args.note === "string" ? args.note : "";
}

// The ONLY place HTML from the server is inserted. The station sanitizes it (nh3) first.
function setSanitizedHtml(target, html) {
  target.innerHTML = html;
}

// ---------- feed ----------

function addFeed(item) {
  // A record that fails verification may arrive as both an unverified detection and a
  // "rejected" message; show it once.
  const last = state.feed[0];
  if (
    item.kind === "rejected" &&
    last &&
    last.kind === "rejected" &&
    last.nodeId === item.nodeId &&
    performance.now() - last.wall < 3000
  ) {
    if (item.reason && item.reason !== last.reason) {
      last.reason = item.reason;
      last.el.replaceWith((last.el = feedRow(last)));
    }
    return;
  }
  item.wall = performance.now();
  item.el = feedRow(item);
  state.feed.unshift(item);
  $("feed-list").prepend(item.el);
  while (state.feed.length > FEED_MAX) state.feed.pop().el.remove();
  $("feed-empty").hidden = true;
  if (state.tab !== "feed") state.unseenFeed += 1;
  renderCounts();
}

function feedRow(item) {
  const rejected = item.kind === "rejected";
  const name = nodeName(item.nodeId);
  const sector = state.nodesById.get(Number(item.nodeId))?.sector;
  const what = rejected ? "Rejected" : soundLabel(item.sound);
  const second = rejected
    ? [name, REASONS[item.reason] || titleCase(item.reason || "Unverified")]
    : [name, sector];
  const badge = rejected
    ? el("span", { class: "badge bad", title: "Signature check failed" }, icon("cross"))
    : item.stub
      ? el("span", { class: "tag", title: "Stub record (not signed)" }, "Stub")
      : el("span", { class: "badge ok", title: "Signature verified" }, icon("check"));

  const detail = el(
    "div",
    { class: "feed-detail", hidden: true },
    item.seq !== undefined && item.seq !== null ? el("span", {}, `Record ${item.seq}`) : null,
    item.clip ? el("span", { class: "mono" }, `Clip ${String(item.clip).slice(0, 8)}…`) : null,
    !rejected && item.seq !== undefined
      ? el("button", { class: "btn small", type: "button", onclick: (e) => tamper(e, item) }, "Tamper")
      : null,
  );
  const row = el(
    "li",
    { class: `feed-item ${rejected ? "rejected" : "verified"}`, dataset: { nodeId: item.nodeId } },
    el(
      "button",
      {
        class: "feed-main",
        type: "button",
        "aria-expanded": "false",
        onclick: (e) => {
          const open = detail.hidden;
          detail.hidden = !open;
          e.currentTarget.setAttribute("aria-expanded", String(open));
          if (state.map && state.nodesById.has(Number(item.nodeId))) state.map.select(Number(item.nodeId));
        },
      },
      el("span", { class: `dot ${rejected ? "red" : "amber"}` }),
      el(
        "span",
        { class: "feed-text" },
        el("span", { class: "line1" }, el("span", { class: "what" }, what), rejected ? null : el("span", { class: "conf" }, percent(item.confidence))),
        el("span", { class: "line2" }, second.filter(Boolean).join(" · ")),
      ),
      el("span", { class: "feed-side" }, timeEl(item.t), badge),
    ),
    detail,
  );
  return row;
}

async function tamper(e, item) {
  e.stopPropagation();
  try {
    await api(`/api/dev/tamper/${encodeURIComponent(item.nodeId)}/${encodeURIComponent(item.seq)}`, { method: "POST" });
    toast("Tampered");
  } catch {
    toast("Unavailable");
  }
}

function onDetection(m) {
  showDetection(m.detection || {}, !!m.stub, true);
}

// live: flash the node. History rows (loaded on page load/reconnect) just fill the feed.
function showDetection(d, stub, live) {
  const verified = d.verified === true;
  const key = `${d.node_id}:${d.seq}:${verified}`;
  if (d.seq !== undefined && state.seenDetections.has(key)) return;
  state.seenDetections.add(key);
  const item = {
    kind: verified ? "detection" : "rejected",
    t: d.timestamp ?? simNow(),
    nodeId: d.node_id,
    sound: d.sound,
    confidence: d.confidence,
    seq: d.seq,
    clip: d.clip_hash,
    stub,
    reason: verified ? null : "bad_signature",
  };
  addFeed(item);
  if (live && state.map) state.map.flashNode(Number(d.node_id), verified ? "amber" : "red");
  if (verified) state.lastByNode.set(Number(d.node_id), item);
  renderNodeCard();
}

function onRejected(m) {
  const node = state.nodesByName.get(String(m.node || "").toLowerCase());
  const nodeId = node ? node.id : null;
  addFeed({ kind: "rejected", t: simNow(), nodeId, reason: m.reason, seq: m.seq });
  if (nodeId !== null && m.reason === "bad_signature" && state.map) state.map.flashNode(nodeId, "red");
}

// ---------- incidents ----------

function upsertIncident(raw) {
  if (!raw || raw.id === undefined || raw.id === null) return null;
  const id = String(raw.id);
  const inc = state.incidents.get(id) || { id, steps: [], plan: null, planHtml: null };
  for (const key of ["sector", "status", "title", "updated_at"]) {
    if (raw[key] !== undefined && raw[key] !== null) inc[key] = raw[key];
  }
  const plan = raw.plan ?? raw.plan_markdown ?? raw.markdown;
  if (typeof plan === "string") inc.plan = plan;
  if (typeof raw.plan_html === "string") inc.planHtml = raw.plan_html;
  const steps = raw.steps || raw.agent_steps;
  if (Array.isArray(steps)) inc.steps = steps.slice(-STEPS_MAX).map(normalizeStep);
  state.incidents.set(id, inc);
  for (const a of [raw.approval, raw.pending_approval, ...(Array.isArray(raw.approvals) ? raw.approvals : [])]) {
    if (a && a.approval_id) onApproval({ incident_id: id, ...a });
  }
  return inc;
}

function normalizeStep(s) {
  return { agent: s.agent || "commander", summary: String(s.summary ?? s.text ?? ""), t: s.at ?? s.t ?? s.timestamp ?? simNow() };
}

function incidentsChanged(id) {
  renderIncidents();
  if (id !== undefined && state.selected === id) renderDetail();
  renderMapIncidents();
}

function sortedIncidents() {
  const rank = (inc) => (statusClass(inc) === "s-urgent" ? 0 : isClosed(inc.status) ? 2 : 1);
  return [...state.incidents.values()].sort(
    (a, b) => rank(a) - rank(b) || (toSeconds(b.updated_at) || 0) - (toSeconds(a.updated_at) || 0),
  );
}

function renderIncidents() {
  const list = sortedIncidents();
  $("incident-list").replaceChildren(...list.map(incidentRow));
  $("incidents-empty").hidden = list.length > 0;
  renderCounts();
}

function incidentRow(inc) {
  return el(
    "li",
    {},
    el(
      "button",
      { class: `incident-row ${statusClass(inc)}`, type: "button", onclick: () => openIncident(inc.id) },
      el(
        "span",
        { class: "incident-text" },
        el("span", { class: "incident-title" }, inc.title || "Incident"),
        el(
          "span",
          { class: "incident-meta" },
          inc.sector ? el("span", { class: "sector" }, inc.sector) : null,
          el("span", { class: "status" }, hasPendingApproval(inc.id) ? "Awaiting approval" : statusLabel(inc.status)),
        ),
      ),
      inc.updated_at !== undefined ? timeEl(inc.updated_at) : null,
      icon("chevronRight", "icon chevron"),
    ),
  );
}

function renderDetail() {
  const view = $("view-incident");
  const inc = state.incidents.get(state.selected);
  const back = el(
    "button",
    { class: "back", type: "button", onclick: () => closeIncident() },
    icon("chevronLeft"),
    "Incidents",
  );
  if (!inc) {
    view.replaceChildren(back, el("p", { class: "empty" }, "Not found"));
    return;
  }
  const plan = inc.planHtml
    ? (() => {
        const d = el("div", { class: "plan rendered" });
        setSanitizedHtml(d, inc.planHtml);
        return d;
      })()
    : inc.plan
      ? el("pre", { class: "plan" }, inc.plan)
      : el("p", { class: "muted" }, "—");
  const steps = inc.steps.length
    ? el(
        "ol",
        { class: "steps" },
        ...[...inc.steps].reverse().map((s) =>
          el(
            "li",
            { class: "step" },
            el("span", { class: `agent agent-${String(s.agent).replace(/[^a-z-]/gi, "")}` }, AGENTS[s.agent] || titleCase(s.agent)),
            el("span", { class: "step-text" }, s.summary),
            timeEl(s.t),
          ),
        ),
      )
    : el("p", { class: "muted" }, "—");

  view.replaceChildren(
    back,
    el("h2", { class: "detail-title" }, inc.title || "Incident"),
    el(
      "div",
      { class: "detail-meta" },
      inc.sector ? el("span", { class: "sector" }, inc.sector) : null,
      el("span", { class: `status ${statusClass(inc)}` }, hasPendingApproval(inc.id) ? "Awaiting approval" : statusLabel(inc.status)),
      inc.updated_at !== undefined ? timeEl(inc.updated_at) : null,
    ),
    el("section", { class: "detail-section" }, el("h3", {}, "Plan"), plan),
    el("section", { class: "detail-section" }, el("h3", {}, "Activity"), steps),
    el("button", { class: "btn wide", type: "button", onclick: () => openCaseFile(inc) }, icon("doc"), "Case file"),
  );
}

function renderMapIncidents() {
  if (!state.map) return;
  state.map.setIncidents(
    [...state.incidents.values()]
      .filter((inc) => !isClosed(inc.status) || inc.id === state.selected)
      .map((inc) => ({ sector: inc.sector, selected: inc.id === state.selected, urgent: statusClass(inc) === "s-urgent" })),
  );
}

async function openCaseFile(inc) {
  const dialog = $("casefile");
  const body = $("casefile-body");
  $("casefile-title").textContent = inc.title || "Case file";
  body.replaceChildren(el("p", { class: "empty" }, "Loading"));
  if (!dialog.open) dialog.showModal();
  try {
    const data = await api(`/api/incidents/${encodeURIComponent(inc.id)}/case_file`);
    if (data && typeof data.html === "string" && data.html.trim()) setSanitizedHtml(body, data.html);
    else body.replaceChildren(el("p", { class: "empty" }, "Not ready"));
  } catch (e) {
    body.replaceChildren(el("p", { class: "empty" }, e instanceof HttpError && e.status === 404 ? "Not ready" : "Unavailable"));
  }
}

function openIncident(id, push = true) {
  state.selected = String(id);
  if (push) history.pushState({ incident: state.selected }, "", `/incident/${encodeURIComponent(state.selected)}`);
  setTab("incidents");
  renderDetail();
  renderMapIncidents();
  const inc = state.incidents.get(state.selected);
  if (inc && inc.sector && state.map) state.map.focusSector(inc.sector);
  document.title = `${inc?.title || "Incident"} · Forest Guardian`;
  $("panel").scrollTop = 0;
  api(`/api/incidents/${encodeURIComponent(state.selected)}`)
    .then((raw) => {
      const got = upsertIncident(raw?.incident || raw);
      if (got) {
        incidentsChanged(got.id);
        if (got.sector && state.map && !inc) state.map.focusSector(got.sector);
        document.title = `${got.title || "Incident"} · Forest Guardian`;
      }
    })
    .catch(() => {});
}

function closeIncident(push = true) {
  state.selected = null;
  if (push && location.pathname !== "/") history.pushState({}, "", "/");
  document.title = "Forest Guardian";
  setTab("incidents");
  renderMapIncidents();
}

function routeFromLocation() {
  const m = location.pathname.match(/^\/incident\/([^/]+)\/?$/);
  if (m) {
    let id;
    try {
      id = decodeURIComponent(m[1]);
    } catch {
      id = m[1];
    }
    openIncident(id, false);
  } else if (state.selected !== null) closeIncident(false);
}

// ---------- approvals ----------

function onApproval(m) {
  if (m.approval_id === undefined || m.approval_id === null) return;
  const id = String(m.approval_id);
  const status = String(m.status || m.state || "pending");
  if (!/pending|open|wait/i.test(status)) {
    removeApproval(id);
    return;
  }
  if (state.approvals.has(id)) return;
  const a = { id, incidentId: String(m.incident_id ?? ""), question: String(m.question || ""), action: m.action, phase: "pending" };
  a.el = approvalCard(a);
  state.approvals.set(id, a);
  $("approvals").append(a.el);
  layoutApprovals();
  renderIncidents();
  if (state.selected === a.incidentId) renderDetail();
  renderMapIncidents();
}

function removeApproval(id) {
  const a = state.approvals.get(id);
  if (!a) return;
  state.approvals.delete(id);
  a.el.classList.add("leaving");
  setTimeout(() => {
    a.el.remove();
    layoutApprovals();
  }, 220);
  renderIncidents();
  if (state.selected === a.incidentId) renderDetail();
  renderMapIncidents();
}

function approvalCard(a) {
  const inc = state.incidents.get(a.incidentId);
  const sector = inc?.sector || a.action?.args?.sector;
  const note = el("textarea", { class: "ap-note", rows: 3, maxlength: 500, placeholder: "Note", "aria-label": "Note" });
  note.value = actionNote(a.action);
  const editBox = el("div", { class: "ap-edit", hidden: true }, note);
  const result = el("div", { class: "ap-result", hidden: true });

  const approve = el("button", { class: "btn primary", type: "button", onclick: () => decide(a, "approve") }, "Approve");
  const edit = el("button", { class: "btn", type: "button", onclick: () => setEditing(true) }, "Edit");
  const reject = el("button", { class: "btn danger", type: "button", onclick: () => decide(a, "reject") }, "Reject");
  const cancel = el("button", { class: "btn", type: "button", hidden: true, onclick: () => setEditing(false) }, "Cancel");
  const send = el("button", { class: "btn primary", type: "button", hidden: true, onclick: () => decide(a, "edit", note.value.trim()) }, "Send");
  const buttons = el("div", { class: "ap-buttons" }, reject, edit, cancel, approve, send);

  function setEditing(on) {
    editBox.hidden = !on;
    for (const b of [approve, edit, reject]) b.hidden = on;
    for (const b of [cancel, send]) b.hidden = !on;
    if (on) note.focus();
  }

  a.setBusy = (busy) => {
    for (const b of [approve, edit, reject, cancel, send]) b.disabled = busy;
    note.disabled = busy;
  };
  a.showResult = (text) => {
    result.replaceChildren(icon("check"), text);
    result.hidden = false;
    buttons.hidden = true;
    editBox.hidden = true;
  };

  const title = inc?.title;
  return el(
    "div",
    { class: "approval", role: "group", "aria-label": "Approval", dataset: { approvalId: a.id } },
    el(
      "div",
      { class: "ap-head" },
      el("span", { class: "ap-kicker" }, icon("shield"), actionText(a.action) || "Approval"),
      sector ? el("span", { class: "sector" }, sector) : null,
    ),
    title
      ? el("button", { class: "ap-title", type: "button", onclick: () => openIncident(a.incidentId) }, title)
      : null,
    a.question ? el("p", { class: "ap-question" }, a.question) : null,
    editBox,
    buttons,
    result,
  );
}

async function decide(a, decision, note = "") {
  if (a.phase !== "pending") return;
  a.phase = "sending";
  a.setBusy(true);
  try {
    await api(`/api/approvals/${encodeURIComponent(a.id)}`, { method: "POST", body: { decision, note } });
    a.phase = "done";
    a.showResult(DECIDED[decision]);
    setTimeout(() => removeApproval(a.id), 1400);
  } catch (e) {
    if (e instanceof HttpError && (e.status === 404 || e.status === 409)) {
      toast("Already handled");
      a.phase = "done";
      removeApproval(a.id);
    } else {
      toast("Not sent");
      a.phase = "pending";
      a.setBusy(false);
    }
  }
}

function layoutApprovals() {
  const box = $("approvals");
  box.hidden = state.approvals.size === 0 && !box.children.length;
  requestAnimationFrame(() => {
    document.body.style.setProperty("--approvals-h", `${box.hidden ? 0 : box.offsetHeight}px`);
  });
}

// ---------- weather, drone, node card ----------

function onWeather(m) {
  if (!m.sector) return;
  if (m.rain) state.rain.add(m.sector);
  else state.rain.delete(m.sector);
  if (state.map) state.map.setRain(m.sector, !!m.rain);
  const chip = $("rain");
  chip.hidden = state.rain.size === 0;
  if (!chip.firstChild) chip.append(icon("drop"), el("span", { class: "chip-text" }, "Rain"));
  chip.title = "Rain";
  renderNodeCard();
}

function onDrone(m) {
  state.drone = m;
  if (state.map) state.map.setDrone(m);
  const chip = $("drone-chip");
  const battery = Math.round(Number(m.battery_pct) || 0);
  const show = m.flying || battery < 99;
  const label = m.flying ? "Drone" : "Charging";
  const relayout = chip.hidden === show || chip.dataset.label !== label;
  chip.hidden = !show;
  chip.dataset.label = label;
  if (show) {
    const bar = el("span", { class: "battery" }, el("span", { class: "battery-fill" }));
    bar.firstChild.style.width = `${Math.max(4, battery)}%`;
    bar.classList.toggle("low", battery < 30);
    chip.replaceChildren(el("span", { class: "drone-chip-label" }, label), bar, el("span", { class: "drone-chip-pct" }, `${battery}%`));
  }
  if (relayout && state.map) state.map.invalidateOverlays();
}

function onSelectNode(node) {
  state.selectedNode = node ? node.id : null;
  renderNodeCard();
}

function renderNodeCard() {
  const card = $("node-card");
  const node = state.selectedNode === null ? null : state.nodesById.get(state.selectedNode);
  const wasHidden = card.hidden;
  card.hidden = !node;
  if (wasHidden !== card.hidden && state.map) state.map.invalidateOverlays();
  if (!node) return;
  const last = state.lastByNode.get(node.id);
  card.replaceChildren(
    el(
      "div",
      { class: "node-card-head" },
      el("strong", {}, node.name),
      el("button", { class: "icon-btn small", type: "button", "aria-label": "Close", onclick: () => state.map.select(null) }, icon("cross")),
    ),
    el("div", { class: "node-card-meta" }, [node.sector, state.rain.has(node.sector) ? "Rain" : null].filter(Boolean).join(" · ")),
    el(
      "div",
      { class: "node-card-last" },
      last ? `${soundLabel(last.sound)} ${percent(last.confidence)}` : "Quiet",
      last ? el("span", { class: "muted" }, " · ", timeEl(last.t)) : null,
    ),
  );
}

// ---------- sim controls ----------

function applySimState(s) {
  if (!s) return;
  if (Array.isArray(s.scenarios)) {
    state.scenarios = s.scenarios;
    renderScenarioOptions();
  }
  onClock(s);
  if (s.drone) onDrone(s.drone);
  if (Array.isArray(s.weather)) {
    for (const sector of [...state.rain]) onWeather({ sector, rain: false });
    for (const w of s.weather) onWeather(w);
  }
}

function onClock(m) {
  Object.assign(state.sim, {
    sim_time: Number(m.sim_time) || state.sim.sim_time,
    speed: Number(m.speed) || state.sim.speed,
    paused: !!m.paused,
    running: !!m.running,
    scenario: m.scenario ?? state.sim.scenario,
    feed: m.feed || state.sim.feed,
    at: performance.now(),
  });
  renderSimControls();
  refreshTimes();
}

function onScenario(m) {
  // A new night: clear what belonged to the previous run.
  for (const item of state.feed) item.el.remove();
  state.feed = [];
  state.unseenFeed = 0;
  state.lastByNode.clear();
  $("feed-empty").hidden = false;
  if (state.map) state.map.clearNodes();
  state.sim.scenario = m.name;
  if ($("scenario").value !== m.name && [...$("scenario").options].some((o) => o.value === m.name)) {
    $("scenario").value = m.name;
  }
  renderCounts();
  renderNodeCard();
}

function renderScenarioOptions() {
  const select = $("scenario");
  const current = select.value || state.sim.scenario || prefs.get("scenario", "night1");
  select.replaceChildren(...state.scenarios.map((s) => el("option", { value: s.name }, s.title || s.name)));
  if (state.scenarios.some((s) => s.name === current)) select.value = current;
}

function renderSimControls() {
  const s = state.sim;
  const selected = $("scenario").value;
  const live = s.running && s.scenario === selected;
  const playing = live && !s.paused;
  const play = $("play");
  play.replaceChildren(icon(playing ? "pause" : "play"));
  play.setAttribute("aria-label", playing ? "Pause" : "Play");
  play.classList.toggle("active", playing);
  $("restart").hidden = !s.scenario;
  const speeds = [...$("speed").options].map((o) => Number(o.value));
  const nearest = speeds.reduce((a, b) => (Math.abs(b - s.speed) < Math.abs(a - s.speed) ? b : a));
  if (s.scenario && document.activeElement !== $("speed")) $("speed").value = String(nearest);
  const sub = $("clock-speed");
  sub.textContent = !s.scenario ? "" : s.paused ? (s.running ? "Paused" : "Ended") : `${nearest}×`;
  sub.dataset.kind = s.scenario && !s.paused ? "speed" : "state";
  $("stub").hidden = !(s.scenario && s.feed === "stub");
}

async function onPlay() {
  const s = state.sim;
  const name = $("scenario").value;
  try {
    if (name === s.scenario && s.running) {
      applySimState(await api("/api/sim/pause", { method: "POST", body: { paused: !s.paused } }));
    } else {
      await startScenario(name);
    }
  } catch {
    toast("Unavailable");
  }
}

async function startScenario(name) {
  if (!name) return;
  const speed = Number($("speed").value) || 30;
  applySimState(await api("/api/sim/scenario", { method: "POST", body: { name, speed } }));
}

function bindControls() {
  $("play").addEventListener("click", onPlay);
  $("restart").append(icon("restart"));
  $("restart").addEventListener("click", () => startScenario($("scenario").value).catch(() => toast("Unavailable")));
  $("scenario").addEventListener("change", (e) => {
    prefs.set("scenario", e.target.value);
    renderSimControls();
  });
  $("speed").value = String(prefs.get("speed", 30));
  $("speed").addEventListener("change", async (e) => {
    const speed = Number(e.target.value);
    prefs.set("speed", speed);
    if (!state.sim.scenario) return;
    try {
      applySimState(await api("/api/sim/speed", { method: "POST", body: { speed } }));
    } catch {
      toast("Unavailable");
    }
  });
  const toggle = $("controls-toggle");
  toggle.append(icon("sliders"));
  const setOpen = (open) => {
    $("bar").classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle.addEventListener("click", () => setOpen(!$("bar").classList.contains("open")));
  document.addEventListener("pointerdown", (e) => {
    if ($("bar").classList.contains("open") && !$("bar").contains(e.target)) setOpen(false);
  });
  for (const tab of document.querySelectorAll(".segmented [data-tab]")) {
    tab.addEventListener("click", () => {
      if (tab.dataset.tab === "incidents" && state.selected !== null) closeIncident();
      setTab(tab.dataset.tab);
    });
  }
  $("casefile-close").append(icon("cross"));
  $("casefile-close").addEventListener("click", () => $("casefile").close());
  $("casefile").addEventListener("click", (e) => {
    if (e.target === $("casefile")) $("casefile").close();
  });
  window.addEventListener("popstate", routeFromLocation);
}

function setTab(tab) {
  state.tab = tab;
  prefs.set("tab", tab);
  $("view-incidents").hidden = tab !== "incidents" || state.selected !== null;
  $("view-incident").hidden = tab !== "incidents" || state.selected === null;
  $("view-feed").hidden = tab !== "feed";
  $("view-results").hidden = tab !== "results";
  for (const b of document.querySelectorAll(".segmented [data-tab]")) {
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  }
  if (tab === "feed") state.unseenFeed = 0;
  renderCounts();
}

// ---------- results (eval/results.json via /api/eval/latest) ----------

const MODE_LABELS = { mock: "Mock", ollama: "Local" };

function resultsTable(r) {
  const total = r.real_incidents.length;
  const b = r.baseline;
  const c = r.commander;
  const row = (name, alerts, caught, missed, wrong) =>
    el("tr", {}, el("th", { scope: "row" }, name), ...[alerts, `${caught}/${total}`, missed, wrong].map((v) => el("td", {}, String(v))));
  return el(
    "table",
    { class: "res-table" },
    el("thead", {}, el("tr", {}, el("td"), ...["Alerts", "Caught", "Missed", "False"].map((h) => el("th", { scope: "col" }, h)))),
    el(
      "tbody",
      {},
      row("Baseline", b.alerts, b.real_caught, b.real_missed, b.false_alerts),
      row("Commander", c.requests, c.real_caught, c.real_missed, c.false_requests),
    ),
  );
}

function resultsCard(r, nights) {
  const c = r.commander;
  const waits = r.pace === "waits";
  const live = Number.isFinite(r.speed) ? `live ${r.speed}×` : "live";
  const who = [MODE_LABELS[r.mode] || r.mode, waits ? "sim waits" : r.mode === "mock" ? "" : live, r.partial ? "partial" : ""];
  const parts = [el("h3", {}, ["Night 1", ...who].filter(Boolean).join(" · ")), resultsTable(r)];
  parts.push(el("p", { class: "res-line" }, `${c.wall_s_per_incident.toFixed(1)} s per incident`));
  const suffix = waits ? ":waits" : "";
  const fresh = nights[`night2:${r.mode}:fresh${suffix}`]?.commander;
  const kept = nights[`night2:${r.mode}:carried${suffix}`]?.commander;
  if (fresh && kept) {
    parts.push(el("p", { class: "res-line" }, `Memory: ${fresh.triage_steps_total} → ${kept.triage_steps_total} triage steps`));
  }
  return el("article", { class: "res-card" }, ...parts);
}

function renderResults() {
  const runs = state.results?.runs || {};
  const cards = Object.values(runs)
    .filter((r) => r.scenario === "night1" && r.memory === "fresh")
    .sort((a, b) => (a.mode === "ollama" ? 0 : 1) - (b.mode === "ollama" ? 0 : 1) || (a.pace === "waits" ? 0 : 1) - (b.pace === "waits" ? 0 : 1))
    .map((r) => resultsCard(r, runs));
  $("tab-results").hidden = cards.length === 0;
  $("view-results").replaceChildren(...cards);
  if (cards.length === 0 && state.tab === "results") setTab("incidents");
}

async function loadResults() {
  try {
    state.results = await api("/api/eval/latest");
  } catch {
    state.results = null; // 404: nothing evaluated yet, tab stays hidden
  }
  renderResults();
}

function renderCounts() {
  const open = [...state.incidents.values()].filter((i) => !isClosed(i.status)).length;
  const ci = $("count-incidents");
  ci.hidden = open === 0;
  ci.textContent = String(open);
  const cf = $("count-feed");
  cf.hidden = state.unseenFeed === 0;
  cf.textContent = state.unseenFeed > 99 ? "99+" : String(state.unseenFeed);
}

// ---------- connection ----------

let socket = null;
let retries = 0;
let offlineTimer = 0;

function setConnected(ok) {
  clearTimeout(offlineTimer);
  if (ok) $("conn").hidden = true;
  else offlineTimer = setTimeout(() => ($("conn").hidden = false), 1500);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws`);
  socket.addEventListener("open", () => {
    retries = 0;
    setConnected(true);
    resync();
  });
  socket.addEventListener("message", (e) => {
    document.body.dataset.live = "1"; // first message: the server is subscribed for us
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    try {
      handle(msg);
    } catch (err) {
      console.error("bad message", msg?.type, err);
    }
  });
  socket.addEventListener("close", () => {
    delete document.body.dataset.live;
    setConnected(false);
    setTimeout(connect, Math.min(10_000, 500 * 2 ** retries++));
  });
}

function handle(m) {
  switch (m && m.type) {
    case "clock":
      return onClock(m);
    case "scenario":
      return onScenario(m);
    case "detection":
      return onDetection(m);
    case "rejected":
      return onRejected(m);
    case "weather":
      return onWeather(m);
    case "drone":
      return onDrone(m);
    case "incident": {
      const inc = upsertIncident(m.incident);
      return inc && incidentsChanged(inc.id);
    }
    case "plan": {
      const inc = upsertIncident({ id: m.incident_id });
      if (!inc) return;
      inc.plan = typeof m.markdown === "string" ? m.markdown : inc.plan;
      inc.planHtml = typeof m.html === "string" ? m.html : inc.planHtml;
      return incidentsChanged(inc.id);
    }
    case "agent_step": {
      const inc = upsertIncident({ id: m.incident_id });
      if (!inc) return;
      inc.steps.push(normalizeStep(m));
      if (inc.steps.length > STEPS_MAX) inc.steps.shift();
      return incidentsChanged(inc.id);
    }
    case "approval":
      return onApproval(m);
    default:
      return undefined;
  }
}

async function resync() {
  try {
    applySimState(await api("/api/sim/state"));
  } catch {
    /* sim routes missing: the map still works from bus messages */
  }
  try {
    // Recent detections, newest first from the API; add oldest first so the feed keeps its order.
    const data = await api("/api/detections?limit=50");
    for (const d of [...(data?.detections || [])].reverse()) showDetection(d, false, false);
  } catch {
    /* no detections API: the feed fills from live messages */
  }
  try {
    const data = await api("/api/incidents");
    const list = Array.isArray(data) ? data : data?.incidents || [];
    for (const raw of list) upsertIncident(raw);
    incidentsChanged(state.selected ?? undefined);
  } catch {
    /* agent not running yet */
  }
}

async function loadHealth() {
  try {
    const data = await api("/api/health");
    // Phones show just the model name ("qwen3:8b" rather than "Local · qwen3:8b").
    const short = String(data.model).split(" · ").pop();
    if (short === data.model) $("model").textContent = data.model;
    else {
      $("model").replaceChildren(
        el("span", { class: "model-long" }, data.model),
        el("span", { class: "model-short" }, short),
      );
    }
    $("model").title = `Model: ${data.model}`;
    $("model").hidden = false;
  } catch {
    setConnected(false);
  }
}

async function loadWorld() {
  for (;;) {
    try {
      return await api("/api/world");
    } catch {
      setConnected(false);
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}

async function init() {
  bindControls();
  loadHealth();
  state.world = await loadWorld();
  setConnected(true);
  for (const n of state.world.nodes) {
    state.nodesById.set(n.id, n);
    state.nodesByName.set(n.name.toLowerCase(), n);
  }
  state.map = new ForestMap($("map"), state.world, {
    onSelectNode,
    showLabels: prefs.get("labels", false) === true,
    onLabelsToggle: (on) => prefs.set("labels", on),
  });
  setTab(state.tab);
  loadResults();
  renderSimControls();
  layoutApprovals();
  new ResizeObserver(layoutApprovals).observe($("approvals"));
  routeFromLocation();
  connect();
  setInterval(refreshTimes, 1000);
  setInterval(() => ($("clock-time").textContent = clockText(simNow())), 250);
  document.body.classList.add("ready");
}

init();
