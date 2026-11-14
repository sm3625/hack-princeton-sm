# Implementation plan: Forest Guardian superagent

Status: partially implemented. The requested rain/activity, unavailable-drone, contradictory-camera,
and ranger-correction slice is documented in `docs/INVESTIGATION_BEHAVIORS.md`; the remaining
phases and full release gate below are still proposed. Builds on `WOLFRAM_AND_SUPERAGENT_REVIEW.md` and the existing commander. This is the implementation sequence for the agent upgrade, not a replacement for the broader `PLAN.md`.

## Outcome

Build an incident commander that can investigate uncertain events, choose useful checks, revise its explanation, and coordinate a response. Preserve the existing deepagents architecture, signed detections, incident threads, local model option, approval gate and UI.

The central behaviour is:

**Observe → compare explanations → choose a useful check → inspect its result → revise the plan → propose a response → learn from the confirmed outcome.**

Creativity means finding a justified next action when evidence is incomplete or contradictory. It does not mean generating unsupported observations, assigning invented probabilities, or bypassing ranger approval.

### Demonstration to build toward

1. A node reports chainsaw-like activity during rain. The commander keeps both rain interference and real activity plausible, then schedules a check.
2. Another node reports sustained compatible activity. The commander revises its assessment and explains what changed.
3. The drone cannot fly because of battery reserve. Instead of treating the failed flight as confirmation, the commander compares waiting with requesting ranger assessment.
4. New evidence or a ranger message changes the chosen response. If dispatch is proposed without camera corroboration, the approval card explicitly states why and what remains unknown.
5. After the ranger supplies the outcome, the commander records a qualified memory and produces a case file separating observations from inferences.

Create a separate contradictory-camera fixture: a successful flight with an inconclusive or contradictory observation must also change the proposal. An empty camera view does not automatically prove no activity occurred.

## Architecture decisions

| Component | Responsibility |
|---|---|
| Commander | Own the investigation, compare candidate actions, update the plan and request approval |
| Existing triage specialist | Produce alternative explanations with supporting/contradicting evidence and suggested checks |
| Existing drone specialist | Inspect feasibility and carry out authorized simulated observation work; return typed outcomes |
| Existing evidence specialist | Produce a cited account from validated records and decision history |
| Deterministic policy and tools | Validate references, enforce budgets, prevent duplicate actions, check route constraints and enforce approvals |
| Optional critic, later | Identify a missing alternative or unsupported assumption on difficult incidents; no action permissions |

Do not start by adding more agents. The three existing specialists are enough for the first implementation. Keep one conversation per incident and deterministic incident grouping. Related incidents may inform an investigation, but automatic merging/splitting is outside the first release.

Keep local Ollama and mock modes. Mock mode validates infrastructure and scripted transitions; only a real-model evaluation can demonstrate reasoning. No paid provider calls, push or deployment are part of this plan's default execution.

## Phase 0 — Establish the baseline and evidence boundary

**Files:** `agent/evidence.py`, `node/runtime.py`, `station/routes_agent.py`, `ui/app.js`, existing evidence and evaluation tests.

- [ ] Run the existing relevant tests and save the current baseline results before behaviour changes. Record pre-existing failures separately.
- [ ] Resolve the captured-clip path mismatch and propagate the configured node-data directory into the commander. Retain compatibility with existing recordings; do not relocate/delete them silently.
- [ ] Distinguish signature validity, clip availability and clip hash match in the API/UI. Missing collection is unknown, not a successful clip verification.
- [ ] Show evidence/citation validation beside the existing case-file view.
- [ ] Capture current mock and local-model replay results with model/configuration identifiers. Use bounded runs and no paid calls.

**Acceptance:** a real captured test clip verifies through the same path used by the station; changed bytes fail; an unavailable clip is labelled unavailable. Existing approval and replay protection tests still pass. Baseline results are available for later comparisons.

## Phase 1 — Make investigation state explicit

**New modules:** `agent/investigation.py` for schemas and validation; `agent/policy.py` for deterministic transition/action checks. Extend the existing incident repository rather than introduce another database.

**Modify:** `agent/incidents.py`, `agent/commander.py`, `agent/tools.py`.

Store a versioned investigation alongside the existing incident. Keep investigation stage separate from the current incident lifecycle status so existing filters and approvals remain compatible.

| Record | Required fields |
|---|---|
| Investigation | Schema version, incident ID, revision, stage, evidence revision, current hypothesis IDs, next action, check deadline |
| Hypothesis | ID, explanation, support references, contradicting references, uncertainty, proposed discriminating check |
| Decision | ID, prior revision, considered actions, chosen action, brief justification, evidence references, expected observation, reconsideration condition |
| Action | Stable ID, type, validated arguments, incident/revision, state, timestamps, result reference |
| Observation | ID, source type, source ID, timestamp, observed content, availability, limitation/coverage metadata |
| Outcome | Ranger-provided result, author/source, timestamp, related incident and supporting references |

- [ ] Add a structured `submit_assessment` tool. Require one leading explanation; require plausible alternatives when ambiguity exists. Do not force false alternatives onto an obvious case.
- [ ] Validate all references against records available to this incident, explicitly retrieved related records, drone observations or ranger messages. Identify each source type; an authentic detection is not verified semantic truth.
- [ ] Reject malformed output with a bounded repair opportunity. After one failed repair, preserve the last valid assessment and surface “assessment unavailable”; do not execute its proposed action.
- [ ] Add revision checks so a stale result cannot overwrite an investigation updated by new evidence.
- [ ] Persist validated decisions transactionally in SQLite. Render `plan.md` and a decision-history file from accepted state; make these generated artifacts code-owned. Let the agent retain a separate scratch file if needed.
- [ ] Centralize the code-owned path list so filesystem permissions and storage logic do not diverge. Regenerate rendered files after a restart if an interrupted write left them behind the database.
- [ ] Give existing incidents a lazy initial investigation state; preserve existing threads, IDs, approvals and files.

**Acceptance:** invalid references and stale revisions are rejected; restart preserves accepted state; generated plans agree with structured state; old incidents still open. The LLM cannot edit authoritative observations or decision history through file tools.

## Phase 2 — Give the agent useful investigation tools

**Files:** `agent/tools.py`, `agent/prompts.py`, `agent/commander.py`, `agent/mock.py`; reuse `sim/drone.py` for route calculations.

- [ ] Add `get_incident_context`: verified incident records, independent node/time summaries, recent observation results, relevant weather, current plan and unresolved questions. Include observation freshness and missing-data limitations.
- [ ] Add bounded `get_related_incidents`: filter by time/distance/family; return a short list and explicit cross-incident references. Nearby records must not silently become proof for the current incident.
- [ ] Add `compare_response_options`: inspect feasible existing drone routes, reserve, arrival time and wait/recharge options from a snapshot. Return typed constraints and assumptions without mutating the simulator or exposing future camera results.
- [ ] Use a fixed action vocabulary: observe by drone, wait/recheck, request ranger input, propose dispatch, dismiss/watch, or close with evidence. Reject invented tools/actions.
- [ ] Rewrite triage to compare explanations and identify missing evidence. Rewrite the commander to choose the check most likely to change its decision, considering urgency, delay, feasibility and resource use.
- [ ] Require a brief comparison of selected versus rejected alternatives on ambiguous cases. Start with qualitative information value; do not fabricate numerical expected utility.
- [ ] Replace the unconditional “drone then dispatch” instruction. Dispatch without camera corroboration may still be proposed when warranted, but must clearly state the evidence, urgency and limitation. Do not make drone availability a universal prerequisite for urgent human review.
- [ ] Update mock policies to exercise the new contracts and explicitly label scripted behaviour in traces.

**Acceptance:** new corroborating evidence revises a rain hypothesis; repeating the same node's correlated records does not create fictitious independent confirmations; an unavailable/contradictory drone result produces a different assessment. All calculations are read-only and refer to current simulator capabilities.

## Phase 3 — Execute bounded investigations without blocking other incidents

**Files:** `agent/commander.py`, `agent/tools.py`, `agent/incidents.py`, `sim/engine.py`, `sim/drone.py` and the existing approval routes.

- [ ] Persist action state: `proposed → pending_approval/queued → running → completed/failed/cancelled`. Use unique action IDs and execution receipts to reject duplicate tool invocations/resumes.
- [ ] Split drone launch from completion. Launch returns a flight/action ID promptly; arrival, refusal or cancellation records an observation and enqueues a wake. Reconcile persisted actions with simulator state on restart; never silently relaunch a flight.
- [ ] Keep one active drone reservation and revalidate battery/no-fly constraints immediately before launch. The simulator remains the authority.
- [ ] Keep agent state updates serialized initially. Non-blocking flights solve the long tool wait without introducing unsafe parallel graph writes. Bound model turn duration so one stalled inference cannot block all incidents indefinitely.
- [ ] Prioritize wakes by explicit urgency and age, with an aging rule to prevent starvation. Do not interrupt an already flying drone merely because a second incident arrives.
- [ ] Add per-wake budgets as configurable starting limits: three decision/check cycles, twelve domain-tool calls, one malformed-output repair, and a wall-time limit tuned to the local machine. Count nested specialist calls too. These are initial caps, not performance claims.
- [ ] At budget exhaustion, retain the best valid state, emit a visible reason, and schedule a bounded recheck or request human input. No recursive retry loop.
- [ ] Preserve the existing interrupt plus consumable approval grant. Bind approvals to the proposed action, target and assessment revision. Materially changed evidence invalidates an obsolete proposal and generates a new review request; rejected proposals are not repeatedly reissued without new justification.
- [ ] Revalidate state on approval resume and record whether dispatch actually executed. An approval alone is not an execution receipt.

**Acceptance:** while one drone flight is underway, a second incident can be assessed; duplicate wakes/approvals do not duplicate actions; stale approvals cannot dispatch a changed proposal; restart/pause/cancellation leaves a visible recoverable state. No dispatch occurs without approval.

## Phase 4 — Learn from ranger outcomes and expose the investigation

**Files:** `agent/tools.py`, `agent/commander.py`, `station/routes_agent.py`, `ui/app.js`, `ui/index.html`, `ui/style.css`.

- [ ] Add a compact “Message ranger station” input inside the existing incident panel, using the existing message endpoint.
- [ ] Add an explicit outcome submission: confirmed threat, authorized activity, false alarm, or unresolved, with a note. An approval/rejection alone must not become a ground-truth outcome.
- [ ] Store memory with provenance, context, timestamp and review/expiry status. Distinguish agent hypotheses from ranger-confirmed outcomes; legacy memory remains unconfirmed context.
- [ ] Make memory retrieval context-sensitive and bounded. A noisy-node history can lower confidence, but must not suppress fresh corroboration automatically. Support correction when a later outcome contradicts a note.
- [ ] Show a concise investigation summary in the existing panel: “Current assessment,” “Next check,” and “What changed.” Put alternative explanations, source references and decision history behind disclosure controls.
- [ ] Include uncertainty and missing corroboration on the approval card. Keep tool IDs/internal state out of user-facing labels.
- [ ] Update case-file generation to separate observations, interpretation, action/approval history and unresolved limitations. Show validation results; do not imply citation validation proves every sentence true.
- [ ] Emit versioned WebSocket updates and include the complete latest state in the incident GET response, so refresh/deep links recover the same panel.

**Acceptance:** a ranger correction wakes the commander and changes its assessment; unconfirmed memory is visibly qualified; a refreshed page retains the plan and pending approval; desktop and mobile flows are usable with escaped model/user text.

## Phase 5 — Prove the improvement

**Files:** `eval/metrics.py`, `eval/run.py`, `eval/report.py`, new scenario fixtures and targeted tests under `tests/`.

### Comparisons

Run the same detection streams through:

1. Existing per-record threshold baseline.
2. Grouped deterministic rules with equivalent weather/node context.
3. Existing commander behaviour.
4. New investigating commander.

Save the current policy/configuration as an explicitly selected evaluation mode before replacing it. Keep it out of the normal product UI. Evaluate classifier performance separately from decision-making, and label synthetic/fallback audio runs.

### Required cases

| Case | Expected behaviour |
|---|---|
| Rain at one noisy node | Watch/recheck unless stronger evidence warrants escalation |
| Real activity during rain | New independent evidence can overturn the initial explanation |
| Contradictory camera result | Reassess; report coverage limitations rather than automatically dismiss |
| Drone unavailable/low battery | Compare feasible alternatives; disclose absence of corroboration |
| Ranger supplies authorization | Revise interpretation and avoid unsupported illegal-activity claims |
| Two incidents share one drone | Assess both; serialize drone use without starving the second incident |
| Duplicate/stale approval | No duplicate or obsolete execution |
| Missing/tampered clip | Accurately report availability/integrity without inventing verification |
| Restart during action/approval | Recover state without silently repeating effects |
| Incorrect historical memory | Fresh evidence and confirmed corrections can override it |

### Metrics and release gate

Measure threat recall, false approval requests, executed dispatches, simulated response delay, plan revisions caused by new evidence, unsupported-claim failures, approval/constraint violations, model calls/tokens where available, tool calls and wall time. Separate inference time from simulated waiting.

Keep scenario truth outside agent-visible context. If evaluation uses an oracle ranger, report proposal quality separately: the oracle's rejection of false requests is not evidence of good agent judgment. Credit actual execution against the intended event, not every true event grouped into an approved incident.

Use controlled arrival barriers or equivalent replay scheduling so model speed does not change the evidence available in a comparison. Freeze the fixture set before tuning and retain held-out variants. Run each local-model case three times initially and report all outcomes; do not claim statistical significance from that small sample.

**Release gate:**

- [ ] All approval, execution-constraint, persistence and integrity regression tests pass.
- [ ] Mandatory adaptation cases show an evidence-appropriate plan change and no invented observations.
- [ ] On the frozen comparison set, threat recall does not fall below the current commander and false approval requests do not increase. At least one of false requests or response delay improves without hiding a regression in the other; report any tradeoff explicitly.
- [ ] No unapproved dispatches or duplicate actions in the fixture suite.
- [ ] Real Ollama runs demonstrate adaptation; mock runs alone do not satisfy the gate.
- [ ] Drive the full demonstration in the browser at desktop and phone sizes; verify refresh, ranger feedback, approval, case-file and error states.

If the agent fails the gate, keep the previous mode available and record the failure. Do not solve a failed evaluation by weakening the baseline or selectively excluding cases.

## Optional after the core passes

**Selective critic:** add one read-only critique for ambiguous/high-impact proposals. Measure whether it reduces mistakes enough to justify latency. Do not add recursive delegation.

**Wolfram:** put an optional backend behind `compare_response_options`. Start with the local deterministic implementation. Add Wolfram only for a substantive sensitivity/optimization question, with bounded calls, explicit assumptions, provenance and offline fallback. It must not become a dependency for classification, signatures or approval. Verify current service access/limits when implementing; no account changes are required for the core plan.

**Richer capabilities:** independent audio review, authorized-work data, calibrated camera coverage, incident merge/split and additional resource scheduling require their own data contracts and evaluations. Do not claim them in the initial release.

## Delivery sequence

Implement Phases 0–2 as the first reviewable slice: reliable evidence, explicit investigation state and demonstrated hypothesis revision. Complete Phases 3–4 for responsive execution and ranger collaboration. Phase 5 is the release gate, with its fixtures developed alongside the corresponding phases rather than postponed to the end.

Use a configuration switch such as `COMMANDER_POLICY=current|investigator` during development. Enable the investigator only after its gate passes; pin policy selection for an active incident so a restart does not silently switch its rules. Preserve the old path until migration and rollback have been checked.

After each slice, report changed behaviour, relevant test results and remaining limitations. Keep existing unrelated working-tree changes intact. Commit only as separately authorized for implementation; never push or deploy without explicit permission.

No user setup is needed to review this plan. Local-model implementation/evaluation requires a working Ollama installation and an available tool-calling model; no Wolfram account or paid API is required.
