// Ranger station UI. Message shapes: common/contracts.py. Text is always set with
// textContent; the only HTML inserted is the server-sanitized case file / plan "html" field.

import { loadGeo, renderInset } from "./geo.js";
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
  const sel = state.selectedNode !== null ? state.nodesById.get(state.selectedNode) : null;
  if (sel && sel.id === Number(d.node_id)) loadRecordings(sel); // new recording on the open node
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
  for (const key of ["sector", "status", "title", "updated_at", "assessment", "ranger_updates"]) {
    if (raw[key] !== undefined && raw[key] !== null) inc[key] = raw[key];
  }
  const plan = raw.plan ?? raw.plan_markdown ?? raw.markdown;
  if (typeof plan === "string") inc.plan = plan;
  if (typeof raw.plan_html === "string") inc.planHtml = raw.plan_html;
  if (Array.isArray(raw.ranger_actions)) inc.rangerActions = raw.ranger_actions;
  if (Array.isArray(raw.trace)) inc.trace = raw.trace.slice(-TRACE_MAX);
  if (Array.isArray(raw.dispatches)) inc.dispatches = raw.dispatches;
  const steps = raw.steps || raw.agent_steps;
  if (Array.isArray(steps)) inc.steps = steps.slice(-STEPS_MAX).map(normalizeStep);
  state.incidents.set(id, inc);
  for (const a of [raw.approval, raw.pending_approval, ...(Array.isArray(raw.approvals) ? raw.approvals : [])]) {
    if (a && a.approval_id) onApproval({ incident_id: id, ...a });
  }
  return inc;
}

const TRACE_MAX = 400;

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
  renderSummary(list);
}

// Three numbers a ranger scans first: what needs a decision, what is live, what was handled.
function renderSummary(list) {
  const box = $("summary");
  if (!box) return;
  const awaiting = list.filter((inc) => hasPendingApproval(inc.id)).length;
  const live = list.filter((inc) => !hasPendingApproval(inc.id) && !isClosed(inc.status)).length;
  const done = list.filter((inc) => isClosed(inc.status)).length;
  const tile = (n, label, cls) =>
    el("div", { class: `sum-tile ${cls}${n ? "" : " zero"}` }, el("span", { class: "sum-n" }, String(n)), el("span", { class: "sum-label" }, label));
  box.replaceChildren(tile(awaiting, "Need you", "t-urgent"), tile(live, "Active", "t-active"), tile(done, "Handled", "t-done"));
  box.hidden = list.length === 0;
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

  const ranger = rangerSection(inc);

  // replaceChildren() turns null into the text "null", so drop the empty sections.
  view.replaceChildren(...[
    back,
    el("h2", { class: "detail-title" }, inc.title || "Incident"),
    el(
      "div",
      { class: "detail-meta" },
      inc.sector ? el("span", { class: "sector" }, inc.sector) : null,
      el("span", { class: `status ${statusClass(inc)}` }, hasPendingApproval(inc.id) ? "Awaiting approval" : statusLabel(inc.status)),
      inc.updated_at !== undefined ? timeEl(inc.updated_at) : null,
    ),
    ranger,
    feedbackSection(inc),
    assessmentSection(inc),
    el("section", { class: "detail-section" }, el("h3", {}, "Plan"), plan),
    decisionTrail(inc, steps),
    el("button", { class: "btn wide", type: "button", onclick: () => openCaseFile(inc) }, icon("doc"), "Case file"),
  ].filter(Boolean));
}

const feedbackDrafts = new Map();
function feedbackSection(inc) {
  const draft = feedbackDrafts.get(inc.id) || { text: "", outcome: "" };
  feedbackDrafts.set(inc.id, draft);
  const note = el("textarea", { class: "ranger-feedback-note", maxlength: "500", required: true,
    placeholder: "What did you observe or confirm?", "aria-label": "Ranger update" });
  const outcome = el("select", { "aria-label": "Ranger outcome" },
    ...[["", "Message / correction"], ["confirmed_threat", "Confirmed threat"],
      ["authorized_activity", "Authorized activity"], ["false_alarm", "False alarm"],
      ["unresolved", "Unresolved"]].map(([value, label]) => el("option", { value }, label)));
  note.value = draft.text;
  outcome.value = draft.outcome;
  note.addEventListener("input", () => { draft.text = note.value; });
  outcome.addEventListener("change", () => { draft.outcome = outcome.value; });
  const status = el("p", { role: "status", class: "muted" });
  const submit = el("button", { type: "submit", class: "btn" }, "Send ranger update");
  const form = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    if (!note.value.trim()) { status.textContent = "Enter a ranger note."; return; }
    submit.disabled = true;
    try {
      await api(`/api/incidents/${encodeURIComponent(inc.id)}/messages`, { method: "POST",
        body: { text: note.value.trim(), outcome: outcome.value || null } });
      note.value = "";
      feedbackDrafts.delete(inc.id);
      status.textContent = "Update saved. Pending proposals will be reassessed; prior dispatches cannot be recalled here.";
      refreshIncident(inc.id);
    } catch { status.textContent = "Could not save the update. Please try again."; }
    finally { submit.disabled = false; }
  } }, outcome, note, submit, status);
  const latest = inc.ranger_updates?.at(-1);
  return el("section", { class: "detail-section" }, el("h3", {}, "Message ranger station"),
    latest ? el("p", {}, "Last ranger report: " + latest.text) : null, form);
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
      const got = upsertIncident(detailToIncident(raw));
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

// ---------- ranger actions ----------

const DECISIONS = { approved: "Approved", rejected: "Rejected", edited: "Approved with edits", auto_rejected: "Repeat request refused" };
const VIA = { imessage: "iMessage", dashboard: "Dashboard" };

function rangerSection(inc) {
  const actions = inc.rangerActions || [];
  const sent = inc.dispatches || [];
  if (!actions.length && !sent.length) return null;
  const rows = [
    ...actions.map((a) =>
      el(
        "li",
        { class: `ranger-action d-${String(a.decision).replace(/[^a-z_]/gi, "")}` },
        el("span", { class: "ranger-what" }, `${DECISIONS[a.decision] || titleCase(a.decision || "")}${a.sector ? ` · ${a.sector}` : ""}`),
        el("span", { class: "ranger-via" }, a.by === "station" ? "Station" : VIA[a.via] ? `Ranger · ${VIA[a.via]}` : "Ranger"),
        el("span", { class: "ranger-time" }, a.time || ""),
        a.note && (a.decision === "rejected" || a.decision === "edited") ? el("span", { class: "ranger-note" }, a.note) : null,
      ),
    ),
    ...sent.map((d) =>
      el(
        "li",
        { class: "ranger-action d-dispatched" },
        el("span", { class: "ranger-what" }, `Rangers dispatched · ${d.sector || ""}`),
        el("span", { class: "ranger-via" }, "Station"),
        el("span", { class: "ranger-time" }, d.time || ""),
      ),
    ),
  ];
  return el("section", { class: "detail-section" }, el("h3", {}, "Ranger"), el("ol", { class: "ranger-actions" }, ...rows));
}

// GET /api/incidents/{id} nests the incident; its plan, decisions and dispatches sit beside it.
function detailToIncident(raw) {
  if (!raw?.incident) return raw;
  return {
    ...raw.incident,
    plan: raw.plan?.markdown,
    plan_html: raw.plan?.html,
    ranger_actions: raw.ranger_actions,
    trace: raw.trace,
    dispatches: raw.dispatches,
    approvals: raw.approvals,
    pending_approval: raw.pending_approval,
  };
}

function refreshIncident(id) {
  api(`/api/incidents/${encodeURIComponent(id)}`)
    .then((raw) => {
      const got = upsertIncident(detailToIncident(raw));
      if (got) incidentsChanged(got.id);
    })
    .catch(() => {});
}

// ---------- approvals ----------

function onApproval(m) {
  if (m.approval_id === undefined || m.approval_id === null) return;
  const id = String(m.approval_id);
  const status = String(m.status || m.state || "pending");
  if (!/pending|open|wait/i.test(status)) {
    removeApproval(id);
    if (m.incident_id !== undefined && String(m.incident_id) === state.selected) refreshIncident(String(m.incident_id));
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
  const next = node ? node.id : null;
  if (next !== state.selectedNode) {
    stopAudio();
    audioUi.listFor = null;
    audioUi.list = [];
  }
  state.selectedNode = next;
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
    liveRow(node),
    recordingsList(node),
  );
  if (audioUi.listFor !== node.id) loadRecordings(node);
}

// ---------- decision trail: how the agent got to its conclusion ----------

const AGENT_INFO = {
  commander: { label: "Commander", cls: "a-commander" },
  triage: { label: "Triage", cls: "a-triage" },
  "drone-pilot": { label: "Drone pilot", cls: "a-drone" },
  evidence: { label: "Evidence", cls: "a-evidence" },
  station: { label: "Station", cls: "a-station" },
  ranger: { label: "Ranger", cls: "a-ranger" },
};
const TOOL_LABEL = {
  get_incident_evidence: "Read the evidence", get_detections: "Searched detections", get_node_info: "Checked a node",
  get_weather: "Checked the weather", compare_response_options: "Compared options", record_assessment: "Recorded an assessment",
  dispatch_rangers: "Requested a ranger team", set_status: "Set the status", schedule_check: "Scheduled a re-check",
  remember: "Saved a lesson", plan_route: "Planned a drone route", fly_drone: "Flew the drone", get_drone_status: "Checked the drone",
  verify_signature: "Verified a signature", write_file: "Wrote notes", edit_file: "Edited notes", read_file: "Read a file",
  ls: "Looked through files", glob: "Looked through files", grep: "Searched files", write_todos: "Updated the to-do list",
};
const DECISION_TOOLS = new Set(["dispatch_rangers", "set_status", "schedule_check", "record_assessment"]);
const MINOR_TOOLS = new Set(["write_file", "edit_file", "read_file", "ls", "glob", "grep", "write_todos"]);
const OUTCOME = {
  open: ["Investigating", "o-open"], watching: ["Watching", "o-watch"], dismissed: ["Dismissed", "o-dismiss"],
  awaiting_approval: ["Waiting for a ranger", "o-ask"], dispatched: ["Team dispatched", "o-dispatch"], closed: ["Closed", "o-closed"],
};

const trailClock = (t) =>
  typeof t === "number" ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
const txt = (v) => (v === undefined || v === null ? "" : String(v));
const agentOf = (e) => AGENT_INFO[e.agent] || { label: titleCase(txt(e.agent) || "agent"), cls: "a-station" };

function decisionTrail(inc, fallback) {
  const trace = inc.trace || [];
  if (!trace.length) {
    return el("section", { class: "detail-section" }, el("h3", {}, "Activity"), fallback);
  }
  const status = hasPendingApproval(inc.id) ? "awaiting_approval" : inc.status;
  const [label, cls] = OUTCOME[status] || [statusLabel(status), "o-open"];
  const outcome = el(
    "div",
    { class: `trail-outcome ${cls}` },
    el("span", { class: "trail-outcome-kicker" }, "Conclusion"),
    el("strong", {}, label),
    inc.summary ? el("p", {}, txt(inc.summary)) : null,
  );
  const legend = el(
    "div",
    { class: "trail-legend" },
    ...["commander", "triage", "drone-pilot", "evidence", "ranger", "station"]
      .filter((a) => a === "commander" || trace.some((e) => e.agent === a))
      .map((a) => el("span", { class: `legend-chip ${AGENT_INFO[a].cls}` }, AGENT_INFO[a].label)),
  );
  return el(
    "section",
    { class: "detail-section trail" },
    el("h3", {}, "How the agent decided"),
    outcome,
    legend,
    el("ol", { class: "trail-blocks" }, ...trailBlocks(trace).map(renderBlock)),
  );
}

// A block starts at each wake-up or ranger decision; sub-agent work between a delegation and
// its result becomes a branch inside the block.
function trailBlocks(trace) {
  const blocks = [];
  let cur = null;
  const start = (head) => {
    cur = { head, items: [] };
    blocks.push(cur);
  };
  for (const e of trace) {
    if (e.kind === "wake" || e.kind === "ranger" || (e.kind === "status" && e.status === "open" && !blocks.length)) start(e);
    else if (!cur) start({ kind: "wake", agent: "station", summary: "Earlier activity", t: e.t });
    else if (e.kind === "status" && e.status === "open") continue; // brief re-open while resuming: noise
    else cur.items.push(e);
  }
  for (const b of blocks) b.items = nest(b.items);
  return blocks;
}

function nest(items) {
  const out = [];
  let branch = null;
  for (const e of items) {
    if (e.kind === "delegate") {
      branch = { kind: "branch", agent: e.target || "sub-agent", t: e.t, items: [], result: null };
      out.push(branch);
    } else if (branch && e.kind === "result") {
      branch.result = e;
      branch = null;
    } else if (branch && e.agent === branch.agent) {
      branch.items.push(e);
    } else {
      out.push(e);
    }
  }
  return out.map(squashMinor);
}

function squashMinor(e) {
  if (e.kind !== "branch") return e;
  return { ...e, items: groupMinor(e.items) };
}

// Collapse runs of file bookkeeping ("wrote notes", "read a file") into one quiet line.
function groupMinor(items) {
  const out = [];
  for (const e of items) {
    const minor = e.kind === "tool" && MINOR_TOOLS.has(e.tool);
    const last = out[out.length - 1];
    if (minor && last && last.kind === "minor") last.items.push(e);
    else out.push(minor ? { kind: "minor", items: [e], t: e.t, agent: e.agent } : e);
  }
  return out;
}

function renderBlock(block) {
  const h = block.head;
  const trigger =
    h.kind === "ranger"
      ? el("span", { class: "trail-trigger t-ranger" }, "Ranger decision")
      : h.kind === "status"
        ? el("span", { class: "trail-trigger t-open" }, "New incident")
        : el("span", { class: "trail-trigger t-wake" }, "Wake-up");
  return el(
    "li",
    { class: "trail-block" },
    el("div", { class: "trail-head" }, trigger, el("span", { class: "trail-head-text" }, txt(h.summary)), el("time", { class: "trail-time" }, trailClock(h.t))),
    el("ol", { class: "trail-flow" }, ...groupMinor(block.items).map(renderItem)),
  );
}

function renderItem(e) {
  if (e.kind === "branch") return renderBranch(e);
  if (e.kind === "minor") {
    return el("li", { class: "flow-node minor" }, el("span", { class: "flow-dot a-station" }),
      el("span", { class: "flow-text muted" }, `${e.items.length} note${e.items.length > 1 ? "s" : ""} written or read`));
  }
  const a = agentOf(e);
  if (e.kind === "status" || e.kind === "approval") {
    const [label, cls] = e.kind === "approval" ? ["Asked a ranger", "o-ask"] : OUTCOME[e.status] || [txt(e.summary), "o-open"];
    return el("li", { class: `flow-node milestone ${cls}` }, el("span", { class: "flow-dot" }),
      el("div", { class: "flow-body" }, el("strong", {}, label), e.kind === "approval" ? el("span", { class: "flow-text" }, txt(e.summary)) : null),
      el("time", { class: "trail-time" }, trailClock(e.t)));
  }
  const decisive = e.kind === "tool" && DECISION_TOOLS.has(e.tool);
  const title = e.kind === "error" ? "Tool failed" : e.tool ? TOOL_LABEL[e.tool] || titleCase(txt(e.tool).replaceAll("_", " ")) : a.label;
  return el(
    "li",
    { class: `flow-node ${decisive ? "decisive" : ""} ${e.kind === "error" ? "error" : ""}` },
    el("span", { class: `flow-dot ${a.cls}` }),
    el("div", { class: "flow-body" },
      el("span", { class: "flow-title" }, el("span", { class: `flow-agent ${a.cls}` }, a.label), title),
      e.summary ? el("span", { class: "flow-text" }, txt(e.summary)) : null),
  );
}

function renderBranch(b) {
  const a = AGENT_INFO[b.agent] || { label: titleCase(txt(b.agent)), cls: "a-station" };
  return el(
    "li",
    { class: `flow-branch ${a.cls}` },
    el("div", { class: "branch-head" }, el("span", { class: `flow-agent ${a.cls}` }, a.label), "sub-agent"),
    el("ol", { class: "trail-flow branch-flow" }, ...b.items.map(renderItem)),
    b.result
      ? el("div", { class: "branch-result" }, el("span", { class: "branch-result-kicker" }, "Reported back"), el("span", {}, txt(b.result.summary)))
      : el("div", { class: "branch-result pending" }, "Working…"),
  );
}

function assessmentSection(inc) {
  const a = inc.assessment;
  if (!a || !(a.explanation || a.reason)) return null;
  const rows = [
    a.explanation ? el("p", {}, txt(a.explanation)) : null,
    a.next_action ? el("p", {}, el("strong", {}, "Next: "), txt(a.next_action).replaceAll("_", " ")) : null,
    a.reason ? el("p", {}, el("strong", {}, "Why: "), txt(a.reason)) : null,
    a.alternative ? el("details", {}, el("summary", {}, "Alternative explanation"), el("p", {}, txt(a.alternative))) : null,
  ];
  return el("section", { class: "detail-section" }, el("h3", {}, "Current assessment"), ...rows);
}

// ---------- node audio: live listening and saved recordings ----------

const audioUi = { audio: null, key: null, status: "", live: null, list: [], listFor: null, loading: false };

function stopAudio() {
  if (audioUi.audio) audioUi.audio.pause();
  audioUi.audio = null;
  audioUi.key = null;
  audioUi.live = null;
}

// Live: play what the node hears, one sound after another; ambient forest sound between events.
function liveRow(node) {
  const on = audioUi.live && audioUi.live.nodeId === node.id;
  const btn = el(
    "button",
    { class: `btn small clip-btn${on ? " playing" : ""}`, type: "button", "aria-pressed": String(Boolean(on)) },
    on ? "Stop listening" : "Listen live",
  );
  btn.addEventListener("click", () => (on ? (stopAudio(), renderNodeCard()) : startLive(node)));
  const what = on ? audioUi.live.label || "Connecting" : "";
  return el("div", { class: "clip-row" }, btn, el("span", { class: "clip-status muted" }, what));
}

const LIVE_LABEL = {
  heard: "Hearing a detected sound",
  "ambient-rain": "Ambient: rain (simulated forest bed)",
  "ambient-night": "Ambient: night insects (simulated forest bed)",
};

function startLive(node) {
  stopAudio();
  audioUi.live = { nodeId: node.id, after: 0, label: "Connecting" };
  renderNodeCard();
  nextLive(node.id);
}

async function nextLive(nodeId) {
  const live = audioUi.live;
  if (!live || live.nodeId !== nodeId) return;
  try {
    const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/live.wav?after=${live.after}`, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const kind = res.headers.get("X-Live-Kind") || "";
    const at = Number(res.headers.get("X-Live-At") || live.after);
    const blobUrl = `/api/nodes/${encodeURIComponent(nodeId)}/live.wav?after=${live.after}`;
    if (audioUi.live !== live) return;
    live.after = Math.max(live.after, at);
    live.label = LIVE_LABEL[kind] || "Listening";
    const audio = new Audio(blobUrl);
    audioUi.audio = audio;
    audio.addEventListener("ended", () => nextLive(nodeId));
    renderNodeCard();
    await audio.play();
  } catch {
    if (audioUi.live === live) {
      live.label = "Live audio unavailable";
      renderNodeCard();
    }
  }
}

// Recordings: every clip the node saved, newest first, each checked against its signed record.
async function loadRecordings(node) {
  audioUi.listFor = node.id;
  audioUi.loading = true;
  try {
    const data = await api(`/api/nodes/${encodeURIComponent(node.id)}/clips`);
    if (audioUi.listFor === node.id) audioUi.list = Array.isArray(data?.clips) ? data.clips : [];
  } catch {
    if (audioUi.listFor === node.id) audioUi.list = [];
  }
  audioUi.loading = false;
  if (state.selectedNode === node.id) renderNodeCard();
}

function recordingsList(node) {
  if (audioUi.listFor !== node.id) return null;
  const items = audioUi.list;
  const head = el("div", { class: "rec-head" }, el("span", {}, "Recordings"),
    el("button", { class: "link-btn", type: "button", onclick: () => loadRecordings(node) }, "Refresh"));
  if (!items.length) return el("div", { class: "recordings" }, head, el("p", { class: "muted rec-empty" }, audioUi.loading ? "Loading" : "No recordings yet"));
  return el(
    "div",
    { class: "recordings" },
    head,
    el(
      "ol",
      { class: "rec-list" },
      ...items.map((c) => {
        const key = `${node.id}:${c.seq}`;
        const playing = audioUi.key === key && audioUi.audio && !audioUi.audio.paused;
        const btn = el("button", { class: `icon-btn small${playing ? " playing" : ""}`, type: "button", "aria-label": playing ? `Pause clip ${c.seq}` : `Play clip ${c.seq}` }, playing ? "❚❚" : "▶");
        btn.addEventListener("click", () => toggleClip(node, c, key));
        const what = c.sound ? `${soundLabel(c.sound)} ${percent(c.confidence)}` : "Recording";
        return el(
          "li",
          { class: "rec-item" },
          btn,
          el("span", { class: "rec-what" }, `#${c.seq} · ${what}`),
          c.timestamp ? timeEl(c.timestamp) : null,
          audioUi.key === key && audioUi.status ? el("span", { class: "rec-status muted" }, audioUi.status) : null,
        );
      }),
    ),
  );
}

async function toggleClip(node, rec, key) {
  if (audioUi.key === key && audioUi.audio) {
    if (audioUi.audio.paused) audioUi.audio.play().catch(() => {});
    else audioUi.audio.pause();
    renderNodeCard();
    return;
  }
  stopAudio();
  const url = `/api/nodes/${encodeURIComponent(node.id)}/clips/${encodeURIComponent(rec.seq)}.wav`;
  audioUi.key = key;
  audioUi.status = "Loading";
  renderNodeCard();
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const hash = res.headers.get("X-Clip-Hash") || "";
    audioUi.status = !rec.clip_hash ? "No signed record for this clip" : hash === rec.clip_hash ? "Matches the signed record ✓" : "Hash does not match the record";
    const audio = new Audio(url);
    for (const ev of ["ended", "pause", "play"]) audio.addEventListener(ev, renderNodeCard);
    audioUi.audio = audio;
    await audio.play();
  } catch {
    audioUi.status = "Clip not available on this node";
  }
  if (audioUi.key === key) renderNodeCard();
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
  document.body.classList.toggle("incident-open", tab === "incidents" && state.selected !== null);
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
      if (m.kind) {
        inc.trace = inc.trace || [];
        inc.trace.push({ ...m, t: m.t ?? simNow() });
        if (inc.trace.length > TRACE_MAX) inc.trace.shift();
      }
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
    // The list carries no approvals: fetch the ones already waiting so their cards show after a reload.
    for (const raw of list) if (raw?.status === "awaiting_approval") refreshIncident(String(raw.id));
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
  loadGeo().then(({ block, reserve }) => {
    state.map.setGeo(block);
    renderInset($("inset-map"), reserve);
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
