# Focused investigation behaviors

Implemented as a focused extension of the existing commander, not the entire proposed
superagent architecture. The current model/provider switch still applies.

## Behavior

- **Activity during rain:** rain remains an alternative explanation, not an exclusion
  rule. Compatible strong reports from separate nodes warrant camera or ranger review.
  The single-node repeat-rain filter remains conservative and never applies to ranger-updated
  incidents. Mock mode uses an explicit scripted fixture rule (same threat class, different
  nodes, confidence at least 0.85, within 120 seconds); this is not a learned calibration.
- **Unavailable drone:** route refusal and cancelled flights are stored as unavailable,
  never as a successful camera observation. The commander compares waiting/rechecking with
  ranger assessment. Strong dry evidence can still support an approval request that states
  the missing aerial verification; ambiguous rainy evidence remains under review.
- **Contradictory/inconclusive camera:** preserve the exact observation and its coverage
  limitation. Reassess before action, usually watch/recheck and request ranger input.
  An empty view does not prove that nothing happened. Code refuses to expose a dispatch
  proposal until a current assessment includes the latest observation and ranger-update counts.
  Two bounded repair attempts are allowed; failure leaves the case watching with a recheck. Real models interpret the observation;
  mock mode recognizes fixture phrases and is explicitly scripted. The station also recognizes
  explicit negative/limited-coverage phrases (such as “inconclusive” or “no activity”) and
  blocks dispatch until ranger confirmation or a new observation. This conservative text rule
  is not a general camera/vision classifier; unrecognized wording still needs model/ranger review.
- **Ranger corrections:** the incident panel accepts a free-form message and optional explicit
  outcome (confirmed threat, authorized activity, false alarm, unresolved). Updates are
  persisted with source and timestamp, wake the commander, and reject superseded pending
  proposals. Explicit clearances update the assessment and plan immediately without a model call;
  obsolete graph interrupts are resumed with rejection only when a new investigation needs them.
  A model assessment from before a correction cannot overwrite it. Explicit
  authorized/false-alarm outcomes block dispatch until a ranger supplies a changed outcome;
  a later ordinary message does not silently remove that restriction. Already executed
  dispatches remain recorded and cannot be recalled through this feature.

`compare_response_options` returns feasibility and recent source material. `record_assessment`
stores an interpretation, alternative explanation, chosen next action and rationale,
with a snapshot of record references and source counts. The UI shows the latest assessment;
full history remains in the incident's SQLite record. Assessments are model interpretations,
not verified observations. The existing consumable approval gate remains mandatory.

## Verification

- `python -m pytest tests/test_investigation_behaviors.py`: scripted end-to-end scenarios,
  correction persistence, obsolete approvals, cancelled flights and stale assessment checks.
- `python -m pytest tests/test_routes_agent.py tests/test_ui.py`: outcome API and feedback
  submission at desktop and phone sizes, including draft preservation across live updates.
- `python -m eval.investigation --mode mock`: four bounded scripted checks.
- `python -m eval.investigation --mode ollama --timeout 180`: four isolated local-model
  checks, with outcomes saved in `eval/investigation_ollama.json`. No paid provider calls.

These smoke tests do not establish accuracy on field recordings. The full superagent's
transactional evidence revisions, action receipts, non-blocking drone flights, resource
budgets and release comparison remain separate work. Drone flights still block the model
worker, as in the existing commander.

## Local-model findings

Initial Qwen checks passed rain and unavailable-drone cases, but exposed skipped camera
reassessment and a slow ranger-correction path. The implementation now enforces current
assessments, explicit camera-review restrictions and immediate human clearances in code.
Earlier failing results are retained alongside the final results; station-policy intervention
is labeled separately from model interpretations. These checks validate the combined system,
not autonomous reasoning accuracy or statistical performance.

Final bounded Qwen-backed system checks: **4/4 passed** (rain, unavailable drone,
inconclusive camera, ranger correction). Camera handling needed the station-policy guard:
the model attempted a dispatch justification, but code kept the incident watching, set a
recheck and exposed no approval. Human confirmation is read from recorded ranger outcomes,
never inferred from model prose. This is evidence that the safeguards work, not that Qwen
independently reasons correctly in every case. Final targeted investigation/simulation run:
78 passed; desktop and mobile ranger-feedback checks also passed.
