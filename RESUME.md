# Resume Here

Read this first in any new session. Then read working-with-me.md, PLAN.md and SPEC.md as needed.
Last updated: 2026-11-14.

## State
- **main** has the pipeline, incident commander, simulator + UI, integration, Phase 6 evaluation and planning/review documents committed through `1f6987f`. The current continuation builds on existing uncommitted evaluation pacing work; do not discard it.
- Current regression suite: **374 passed, 9 skipped, 6 deselected**; lint and `git diff --check` clean. Run `make test` with localhost socket access; sandbox-blocked socket tests are not app failures.
- Agent walkthrough verified on local Ollama `qwen3:8b` (`make test-ollama`: 1 passed, ~172 s). This controlled walkthrough is separate from a full replay score.
- Demo: `make dev` → http://127.0.0.1:8000 → Night 1, 60×, play. Screenshot: docs/screenshots/integrated.png.

## Classifier (Phase 1): merged
- `models/classifier.tflite` (55 KB int8), `models/eval.md` has full numbers. Station picks it automatically.
- Held-out: chainsaw recall 92.9% (only 8 test clips), background false positives 6.9% per window (target ≤5%, NOT met; 3.9% of background clips fire a false event after debounce). Chainsaw precision is low: AC, washing machine, insects, wind get called chainsaw.
- Datasets live only in `.claude/worktrees/agent-a9fde28df4a63e0b2/data/` (~9 GB). Data-dependent tests skip on main.
- Possible improvement: record real chainsaw + venue/forest background clips and retrain (`uv run python -m training.train --val`, then `--bias data/cache/class_bias.json`).

## Phase 6: evaluation implemented; real-model replay needs improvement
- `eval/run.py` starts isolated real-pipeline station processes, supports mock/Ollama, carries memory between nights, and writes results/report. `make eval` runs mock nights. Results are served by `/api/eval/latest` and shown in the existing Results tab.
- Saved Night 1: baseline 29 alerts, 2/2 real incidents; mock 11 requests, 2/2 caught, 8 false requests. Saved Ollama **live 60x**: 0 requests, 0/2 caught. This is a recall regression, not a successful alert reduction.
- Saved Ollama **sim waits** (qwen3:8b, hit the 20 min cap at about sim 04:00, partial): 0 requests, 0/2 caught; 184.7 s commander time per incident. With the clock held the model sees the real chainsaw records and still calls them false alarms (single node, 'no corroboration'), and invents node names. So the miss is model judgment, not only latency.
- Saved mock Night 2 memory comparison: 73 triage steps fresh versus 78 with memory. No demonstrated reduction in effort.
- The prior agent added `--sim-waits`. This continuation fixes a deadlock: pausing for a busy commander also paused the clock needed by its blocking drone tool. Flights now keep advancing, including after scenario end; evaluation also waits for a silent busy commander before declaring completion.
- Verified corrected pacing through the real classifier/signing/station pipeline with a mock agent: `uv run pytest -q -m e2e tests/test_eval_e2e.py -k sim-waits` passed in 305 s, replaying Night 1 from 02:10. Both baseline and commander caught the logging incident. This shortened test does not replace a saved full-night evaluation.
- Reports use recorded replay speed, distinguish live/waiting variants and their memory runs, and show separate baselines when results differ. Regression/memory conclusions are explicit. Existing result data is retained; regenerating the report is not a new replay.
- Browser verification: 14 Playwright UI tests passed, including mobile and distinct live/waiting memory comparisons; inspected desktop/375px screenshots without horizontal overflow.
- `--sim-waits` is pacing assistance, not deterministic lockstep: the clock runs during flights and arrivals can differ between runs. The evaluation still uses an oracle ranger and scores approved requests as catches; see the review before treating these numbers as field performance.

## Local-model fixes (2026-11-14, user chose "option 1"; superagent plan is ON HOLD, do not start it)
- `get_incident_evidence` tool (commander, triage, evidence): every verified record in the incident, code-counted independent nodes, "Corroborated: YES/NO", weather, valid node-name list. The same block is in every wake-up message.
- `get_detections` reaches back to 5 min before the incident start while the incident is open/watching (slow model at 60x); after dispatch the plain window applies so "is it quiet now?" checks still work.
- Triage prompt: call get_incident_evidence first, trust its counts, 2+ independent nodes = corroboration.
- Tests: 2 new in tests/test_agent_tools.py; suite 376 passed. Re-run `eval.run --scenario night1 --mode ollama --sim-waits --max-minutes 45` (partial, hit cap; gunshot incident at 04:37 never reached): 15 requests, 1/2 caught (the logging, E4; baseline also only reached 1), 10 false, 782 s commander time per incident. Old result was 0 requests, 0/2.
- New problems seen: (a) the agent re-requested dispatch for the rain incident B3 ~10 times after each ranger reject; (b) IncidentManager merged the real logging nodes (Creek Bend, Mossy Gap) into the rain incident at Low Marsh/Fern Hollow; (c) the '2+ nodes = real' prompt rule over-trusts rain-correlated nodes. Proposed next code fixes: block re-requests after a reject unless new evidence, a code-computed 'all corroborating nodes in rain' flag, don't merge into an incident across rain/dry or after long gaps.

## Next
1. Run and inspect a bounded full local-model replay with corrected pacing:
   `uv run python -m eval.run --scenario night1 --mode ollama --sim-waits --max-minutes 20`
   Preserve the live result for comparison. Inspect partial status and missed incidents before proceeding to Night 2 fresh/carried runs. A passing walkthrough does not establish successful full-night reasoning.
2. (ON HOLD until the user says so) Improve the agent using `SUPERAGENT_IMPLEMENTATION_PLAN.md`, starting with evidence integrity and structured investigation. `WOLFRAM_AND_SUPERAGENT_REVIEW.md` documents the current limitations. These are plans, not implemented superagent features.
3. **Phase 7 polish**: LangSmith trace only if credentials are available, outside-in security check, rehearse SPEC §6 demo script, record backup video. No paid calls or publishing without permission.
4. Refresh the generated Graphify map when its tooling is available; the checked-in graph describes an older skeleton.

## Known issues
- `tests/...test_runner_in_real_time_at_60x` is timing-sensitive; flaked once under load.
- `/ws` allows only localhost origins, so the UI can't be opened from a phone over LAN.
- Captured-clip layout/configuration and evidence validation display still need the fixes described in the superagent review; not addressed by the evaluation pacing work.
- No commit, push or deployment was performed by this continuation. Existing edits to `eval/results.json` came from the prior agent's runs.

## Standing rules (also in memory)
- Never push without an explicit yes. No AI attribution in commits.
- Cheapest fitting subagent model (haiku/sonnet); opus only for hard work.
- ChatOverflow: search on errors, vote on used notes, save a note for every distinct real insight.
- No paid API calls without the user's OK (Haiku mode costs money).
