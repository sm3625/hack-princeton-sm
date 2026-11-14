# Wolfram and superagent review

Reviewed 14 November 2026. This is a source review, not an implementation or a claim that the unfinished system has passed a new evaluation.

## Recommendation

**Keep Wolfram optional. Improve the commander's decision process first.** The compelling feature is an investigator that compares explanations, chooses a useful observation, and changes its response when the evidence changes. A Wolfram calculation could support that investigation, but adding a calculator alone would not produce it.

The existing “superagent” is a persistent incident coordinator with three specialists, tools, memory files, and a human approval boundary. This is substantial infrastructure. Its present instructions nevertheless steer it through a fairly fixed triage → drone → ranger → evidence workflow. In mock mode, that workflow is explicitly programmed in Python; in real-model modes, an LLM chooses calls within a similarly prescriptive workflow.

If time is short, I would implement hypothesis comparison, evidence-sensitive replanning, and a stronger evaluation before integrating Wolfram. If a Wolfram demonstration is desirable, make it a small, optional **response-options analysis** tool with recorded inputs and assumptions, rather than placing it in the acoustic detection or dispatch path.

## Scope and the intended finished product

I read the first-party source across `agent/`, `common/`, `node/`, `transport/`, `station/`, `sim/`, `training/`, `eval/`, `ui/`, scripts, scenario/configuration files, and tests, along with the project Markdown: `SPEC.md`, `PLAN.md`, `RESUME.md`, `working-with-me.md`, both idea documents, `scenarios/README.md`, `models/eval.md`, and the Graphify report. Dependency implementations, generated caches, binary weights/audio, credentials, and other checkouts were outside this review.

The application is **Acoustic Forest Guardian**. I interpreted “graphiphy” as the Graphify material in `graphify-out/`. Its report and manifest describe an older skeleton: many current agent and evaluation modules are absent from that manifest. I used it for orientation and checked the actual current source rather than treating the generated graph as authoritative.

The intended finished product combines a small real acoustic classifier, signed node detections, low-bandwidth transport, a station-side agent, a simulated drone, human-approved ranger dispatch, and inspectable case files. The specification explicitly excludes precise acoustic localization/TDOA from the core scope. The alternate air-quality idea is not the implemented product.

Some plans are behind the code: evaluation modules now exist even where older planning text still describes work to do. Conversely, targets such as “40 alerts → 2” are not measured results merely because they appear in design documents. I inspected existing test assertions but did not run the application, tests, training, or evaluation, which could write runtime artifacts. I did not verify the user's Wolfram account or make authenticated API calls.

## 1. What the superagent actually does

### The end-to-end boundary

1. **Nodes classify audio locally.** `node/classifier.py` runs an int8 TFLite model on log-mel features. `node/runtime.py` applies confidence thresholds, voting/debounce, duplicate suppression and an hourly transmission budget, retains a short audio clip, and signs a compact detection. The real classifier has background, chainsaw, gunshot and vehicle classes; axe support in enums/synthetic paths does not mean the trained model recognizes chopping.
2. **The station verifies and stores records.** Invalid signatures do not reach the commander hook. A valid signature establishes record authenticity/integrity, not that the classifier was right or that an activity was illegal.
3. **Ordinary code creates incidents.** `IncidentManager` in `agent/incidents.py` groups compatible detections using distance and time thresholds: 1,500 metres and 45 minutes. The location is a centroid weighted by each node's maximum confidence. It is a useful display/response heuristic, not acoustic triangulation or a calibrated source-location posterior.
4. **Ordinary code wakes the agent.** `WakeQueue` batches detections and handles scheduled checks and ranger messages. `Commander` maintains a checkpointed conversation per incident, files, approvals and event reporting.
5. **The agent operates tools.** It delegates triage, requests drone work, proposes ranger dispatch, schedules a follow-up, records memory, and closes out evidence. The UI displays this activity and approval requests.

This division matters: incident grouping, signature verification, pathfinding and execution constraints are already deterministic code. A reduction in alert count is not entirely attributable to LLM reasoning.

### Roles and actual capabilities

The concrete construction is `build_agent()` in `agent/commander.py:129`; permissions are enumerated in `agent/tools.py:373` and prompts in `agent/prompts.py`.

| Role | What it can do | What it cannot currently establish |
|---|---|---|
| Commander | Read detections/node context/weather; delegate; maintain plan/files; schedule checks; change incident status; remember notes; request ranger dispatch | Independently hear a clip, inspect a real camera image, infer exact source coordinates, or execute an unapproved dispatch |
| Triage | Query detections, node history and weather; write triage notes; return a prompted JSON verdict | Reclassify raw audio or derive a statistically calibrated probability of illegal activity |
| Drone pilot | Inspect drone state, calculate a route, execute a simulated flight, report the observation | Operate real hardware or perform computer vision; observations are scenario-provided strings |
| Evidence | Verify records, read incident files, write a case file and memory notes | Prove that every prose assertion is supported just because cited signatures verify |

These are scoped specialists, not independent organizations of agents. The generic subagent is disabled, specialists cannot recursively delegate through `task`, and the project disables the generic `write_todos` tool. It uses its own plan file. No project-specific dynamically loaded procedural skills implement the more expansive idea-document vision.

### Three different meanings of “agent” in this repository

- **Mock:** `agent/mock.py` is a deterministic policy generating framework-compatible tool calls. For example, rain plus one/noisy node yields a false alarm; two nodes generally yield a real verdict. This exercises delegation, persistence and approval mechanics, but is not creative reasoning.
- **Ollama:** `agent/llm.py` uses a real local model; the default configuration names `qwen3:8b` and sets temperature to zero. This can reason and deviate, subject to model capability and tool access.
- **Hosted mode:** the `haiku` branch uses `ChatAnthropic` and the configured model identifier, also at temperature zero. Its presence in source is not evidence that credentials, model availability or current provider support have been verified.

All real roles currently use the same provider/model configuration. They get different instructions and tool access, not complementary trained expertise.

### Persistence is real; learning is narrower

Each incident has a LangGraph thread and SQLite checkpoints. Files include a plan, detection log, triage, drone activity, approvals and case report. The agent is denied writes to the code-owned detection and approval logs.

`FreshMemoryMiddleware` reloads node/sector notes; `remember()` adds short text notes and `get_node_info()` retrieves matching notes and dismissal history. This is persistent textual memory, not model training. It can carry useful local context across incidents, but an incorrect dismissal can also become a misleading reputation for a node. Notes currently lack systematic outcome validation, expiration and provenance.

Background incident invocations share a **single worker** (`agent/commander.py:403`). A simulated flight waits for arrival before returning its observation. Consequently, another incident can wait behind drone work, and a paused simulation can hold that work open. Calling this a globally parallel autonomous swarm would overstate the implementation.

### Where flexibility is currently constrained

The main prompt largely prescribes this sequence: triage new incidents; dismiss false alarms; for real threat sounds, use the drone and then request rangers; watch vehicle/uncertain cases; follow up after dispatch; write evidence when quiet.

The initial `plan.md` is a checklist, not an executable planner. A particularly important inconsistency is that the seed plan talks about confirmation, while the root prompt and mock path can request dispatch after a drone attempt without requiring a corroborating camera observation. An infeasible flight is not itself a reason the mock stops requesting approval. Human approval remains required; this finding concerns the quality of the proposal reaching the human.

The dispatch boundary itself is stronger than a prompt: a framework interrupt and `ApprovalGate` require an approved, consumable grant. Preserve both. In this demo, “dispatch” records the action/state; it does not contact a physical ranger service.

## 2. How to make it meaningfully more creative

Define creativity as **finding a useful, evidence-grounded next action when the obvious workflow does not fit**. More colourful case prose or extra agent roles would not be a sufficient improvement.

### Highest-value change: competing explanations and replanning

For ambiguous or consequential incidents, have the commander maintain two or three alternatives, each with supporting evidence, contradictions and a discriminating next observation. For example:

| Hypothesis | Relevant evidence | What would change the decision? |
|---|---|---|
| Rain-related false detection | One historically noisy node; rain present | A nearby, independently situated node reports sustained activity outside the rain pattern |
| Real logging during rain | Repeated compatible records at multiple nodes | A feasible drone observation corroborates activity, or subsequent independent evidence contradicts it |
| Benign machinery/authorized work | Vehicle/machinery evidence with plausible timing | A ranger supplies authorization context; a permit lookup would require a new data source |

Do not invent numeric posteriors from classifier confidence. Start with ranked explanations and explicit uncertainty. Correlated windows from one node are not independent confirmations.

Then ask: **Which available check most changes the action, for the least delay and resource use?** Choose the check, inspect its result, and revise the plan. Show a compact decision record: observations, alternatives, chosen check, result, changed decision and remaining uncertainty. This is an auditable explanation, not a request to display private chain-of-thought.

A good demo: an initially noisy rain-affected node is joined by a second credible signal. The commander reopens its working explanation instead of treating the first dismissal as permanent truth. A negative or unavailable drone result changes the response and the approval note instead of merely checking off “drone.”

### What can change through prompts, and what requires code

| Priority | Change | Scope |
|---|---|---|
| First | Require alternatives, explicit uncertainty, and replanning after contradictory/unavailable observations | Prompts and plan format can demonstrate this with existing tools; predictable enforcement needs structured state/validation |
| First | Distinguish drone corroboration, contradiction and unavailability in the dispatch proposal | Prompts plus typed tool results/policy checks; do not equate all three outcomes |
| Next | Add incident-scoped verified timelines and related-incident lookup | New read-only tools; current sector queries can include nearby records from other incidents and sound classes |
| Next | Add a non-mutating response-options tool | Compare feasible existing routes, battery margins and waiting/recharge; operate on a snapshot without launching a flight |
| Next | Record confirmed ranger outcomes and revise/expire memory | Data model and UI/API work; separate agent conjecture from human-confirmed history |
| Later | Use a skeptic only for ambiguous/high-impact cases | One selective critique call, with no action permissions; avoid paying the overhead on every obvious event |
| Later | Coordinate priorities across incidents | Explicit shared-resource scheduler and cross-incident state; current per-incident threads do not provide global planning |

The existing ranger-message endpoint could support corrections such as “authorized maintenance in this sector.” The current UI would need a composer to make that capability accessible.

Future observation tools could inspect locally collected clips, run an independent model or consult authorization records. Those are new capabilities, not abilities unlocked by asking the existing agent to “think harder.” The signed low-bandwidth uplink does not carry the raw waveform.

A legal observation point near an unreachable target might be a useful creative option, **but only after camera range/visibility and allowed observation targets are modeled**. The present scenario-string camera cannot validate such a strategy. Likewise, keep hypothetical simulations separate from live state and hide scenario ground-truth labels/future observations from the planner.

Temperature is secondary. Zero temperature does not eliminate reasoning. A modest increase for a hypothesis-generating role could be evaluated, while evidence writing stays conservative; it will not add missing data or tool capabilities. First improve the task and measurements, then compare model/role settings on identical cases.

## 3. Where Wolfram fits—and where it does not

### Free access: distinguish the products

There are several routes, with different deployment implications:

- **Wolfram|Alpha API:** the official API page advertises up to **2,000 free non-commercial calls per month**. The API uses an AppID. This is an API allowance, not an assumption of unlimited access from a consumer or university license. [Official API overview](https://products.wolframalpha.com/api)
- **Wolfram Cloud MCP:** official support currently documents `https://agenttools.wolfram.com/mcp` as free with no authentication. The product page describes limited personal use, stateless requests and no external-file access. Treat it as an optional online demo dependency, not an unlimited production service guarantee. [Connection documentation](https://support.wolfram.com/75237), [Cloud MCP capabilities](https://www.wolfram.com/ai/mcp/cloud/)
- **Wolfram Local MCP:** the official comparison describes local execution and persistent sessions; access requires an appropriate licensed Wolfram installation. It may suit a Wolfram|One/Mathematica entitlement better than the Alpha API. Local computation can work offline; online knowledge queries still require their underlying services. [MCP overview](https://www.wolfram.com/ai/mcp/), [Local MCP](https://www.wolfram.com/ai/mcp/local/)

Your stated free access makes a prototype plausible. The specific entitlement should determine the adapter; I did not inspect your account. Connecting Wolfram to a coding assistant also does not automatically connect it to this application's LangChain agent—the running application needs its own integration.

### Ranked use cases

| Use | Value here | Recommendation |
|---|---|---|
| Compare response choices under explicit assumptions | Potentially useful when there are several meaningful choices or uncertainty ranges | Best optional live feature; richer optimization is more naturally Wolfram Language than a natural-language Alpha query |
| Explore sensor placement or sensitivity to node loss | Useful development/planning analysis | Offline notebook first; requires explicit coverage/propagation assumptions and validation |
| Statistical analysis of held-out classifier performance | Useful given current false positives and limited samples | Development aid; local Python can also do it, so no reason to make incident response depend on it |
| Sunrise/daylight context | Possible context for operations | Small optional feature; use scenario location/date/timezone explicitly and do not infer legality from daylight |
| Existing distance/route/battery arithmetic | Little incremental value | Keep the present deterministic implementation |
| Classify sounds, establish illegal logging, or precisely locate the source | Unsupported by the available inputs | Do not use Wolfram for these claims |

`sim/drone.py` already computes routes around inflated no-fly rectangles and checks return-trip battery reserve. Its simplified physics uses fixed speed and consumption; weather data does not make the flight model wind-aware. An external solver cannot honestly produce a validated wind-adjusted optimum without a new physical model and inputs.

Likewise, the current packets have second-resolution detection timestamps and class confidence, not synchronized acoustic arrival times, bearings or calibrated intensity. A Wolfram “triangulation” feature would be invented precision. A confidence-weighted node centroid should continue to be labelled an estimate.

### A concrete optional integration design

These are proposed changes only; none were implemented for this review.

1. Define a narrow `analyze_response_options` interface in a future `agent/computation.py`. Supply validated route candidates from the existing planner, a snapshot of battery/simulated time, and explicit uncertainty ranges. Return options, assumptions, units, feasibility reasons and sensitivity—not permission to act.
2. Inject the backend through `Services` in `agent/tools.py`; expose the tool through the role allowlist. Provide a local/off implementation and a fixture backend for mock runs. Enable Wolfram explicitly, not as an import-time requirement.
3. Pick one transport. For Alpha, use a server-side adapter and AppID. For Cloud MCP, establish an application-owned MCP client and expose only the needed computational capability. Do not give a numerical helper ranger-dispatch permissions or unrestricted local file access.
4. Prefer typed outputs for calculations. Alpha's Full Results endpoint supports JSON, but it returns result pods and interpretation metadata, not a guaranteed domain-specific numeric schema. Validate interpretation, assumptions, units, missing results and timeouts; do not assume a fixed pod index is the answer. The LLM API is convenient for text exploration, less convenient as a strict numerical contract. [Full Results documentation](https://products.wolframalpha.com/api/documentation), [LLM API documentation](https://products.wolframalpha.com/llm-api/documentation)
5. Bound work: a short timeout, small per-incident call budget, cached identical calculations, explicit quota/error states, and local fallback. Send only necessary numeric/context inputs. Keep an AppID on the server and out of UI/logged URLs.
6. Persist a code-owned calculation record: source record IDs where applicable, inputs and units, scenario timestamp/timezone, assumptions, backend, result, and response hash. Add it to write-denied paths. Derived calculations belong in a separate case-file section; they are not signed sensor evidence.
7. Keep `plan_route`/`fly_drone` execution checks and the ranger approval gate authoritative. Revalidate against current state before acting because a calculation snapshot may be stale.

For a small demonstration, compare “fly now” and “wait for recharge” using the existing model and show arrival time, reserve and assumptions. Be candid that this arithmetic is easily local. Wolfram becomes distinctive only when the demonstration adds a real parameter sweep, constrained optimization or sensitivity question. Avoid building elaborate plumbing to replace a multiplication.

**Why skipping is justified:** the core risk is uncertain evidence and rigid decision-making, not a missing mathematical engine. Cloud calls add latency, quotas and network dependence; broad natural-language queries add interpretation ambiguity. Free access removes one cost, but not those integration costs. An optional planning notebook may deliver more value than a live dependency before the basic evaluation is trustworthy.

## 4. Prerequisites and an honest evaluation

Two evidence issues are worth fixing before giving the agent more freedom:

- **Clip lookup disagrees with capture layout.** `node/runtime.py:65` writes `<node>/<seq>.wav`; `agent/evidence.py:69` searches `<node>/clips/`. An absent folder is reported as “not collected,” and that status does not fail `Integrity.ok`. Also check propagation of the configured node-data root into the commander. Verify a real captured clip against its signed hash end to end before presenting stronger evidence claims.
- **Citation checking is narrower than factual checking.** `check_citations()` checks referenced records and requires a citation, but cannot establish that every assertion is supported. `ui/app.js` renders the case HTML without presenting the separate integrity/citation result fields. Distinguish observed, inferred and unknown statements, and expose validation results alongside the report.

The reported classifier results in `models/eval.md` show meaningful false-positive limitations. Their gunshot debounce evaluation also needs refreshing against the current runtime's two-of-five rule; the older evaluation uses three-of-five. This matters more than adding another reasoner over the same uncertain labels.

Use the existing evaluation harness as a starting point, with these qualifications:

1. **Separate aggregation from intelligence.** Compare per-record baseline, grouped deterministic/weather rules, single-model triage, current commander, and the proposed investigator on identical detection inputs. Only the first baseline makes grouping alone look like agent intelligence.
2. **Separate proposals from outcomes.** `eval/run.py` uses scenario truth to approve or reject requests. `eval/metrics.py` treats approved/edited approvals as dispatches and credits real groups contained in the incident. Low false dispatches under an oracle ranger are not proof of good autonomous judgment. Report false requests, actual executed actions, missed events and response delay separately; require an appropriate link between a response and each credited event.
3. **Distinguish audio realism from pipeline coverage.** Synthetic clips can be generated from the expected detected sound rather than the true physical source. A rain false-positive scenario can therefore exercise a chainsaw-like synthetic signal with rain, rather than prove that rain fooled the real model. Label synthetic/fallback runs and use held-out real audio for classifier claims.
4. **Measure useful effort.** Tool-step counts are not LLM calls/tokens/cost. Wall time can include waiting for a drone; accelerated simulation changes what a slower model sees. Control event arrival or use replay barriers, and report simulated response delay separately from inference and tool time.
5. **Test changed conditions.** Add rain plus genuine activity, noisy-node recovery, contradictory/no camera observation, low battery, simultaneous incidents, authorization context, and packet loss. Compare fresh versus carried memory without letting unverified prior dismissals become ground truth.
6. **Reward grounded adaptation.** Require that changed evidence produces an appropriate changed plan, no fabricated observations enter the case, and no action bypasses execution constraints or human approval. Measure recall and ranger burden together; fewer alerts alone can reward dismissing everything.

My preferred implementation order is: **repair evidence visibility/path consistency → improve hypothesis and replanning behaviour → evaluate against a stronger baseline → add an optional Wolfram-backed comparison only if it changes a useful decision.** That order serves both the finished product described in the Markdown and a more convincing live demonstration.

## Source map

- Intent and scope: `SPEC.md`, `PLAN.md`, `RESUME.md`, `ideas/acoustic-forest-guardian.md`.
- Agent construction, permissions, execution and persistence: `agent/commander.py`, `agent/tools.py`, `agent/incidents.py`.
- Behaviour and model differences: `agent/prompts.py`, `agent/mock.py`, `agent/llm.py`, `station/config.py`.
- Evidence boundaries: `agent/evidence.py`, `node/runtime.py`, `common/contracts.py`, `station/receiver.py`, `station/routes_agent.py`, `ui/app.js`.
- Physical/synthetic constraints: `common/contracts.py`, `sim/drone.py`, `sim/engine.py`, `sim/audio.py`, scenario YAML and `station/integration.py`.
- Measurement: `models/eval.md`, `training/evaluate.py`, `eval/metrics.py`, `eval/run.py`, `eval/report.py`, and the corresponding tests.

Only this new Markdown file was created for the review. Existing working-tree changes were left intact. No implementation, commit, deployment or measured performance claim is included.
