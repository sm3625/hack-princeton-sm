# Resume Here

Read this first in any new session. Then read working-with-me.md, PLAN.md and SPEC.md as needed.
Last updated: 2026-11-14.

## State
- **main** has the pipeline, incident commander, simulator + UI, and integration merged. 342 tests pass (`make test`), lint clean. End-to-end night runs (`make test-e2e`, mock LLM). Nothing pushed.
- Agent verified on local Ollama `qwen3:8b` (`make test-ollama` passes, ~2 min).
- Demo: `make dev` → http://127.0.0.1:8000 → Night 1, 60×, play. Screenshot: docs/screenshots/integrated.png.

## Classifier (Phase 1): merged
- `models/classifier.tflite` (55 KB int8), `models/eval.md` has full numbers. Station picks it automatically.
- Held-out: chainsaw recall 92.9% (only 8 test clips), background false positives 6.9% per window (target ≤5%, NOT met; 3.9% of background clips fire a false event after debounce). Chainsaw precision is low: AC, washing machine, insects, wind get called chainsaw.
- Datasets live only in `.claude/worktrees/agent-a9fde28df4a63e0b2/data/` (~9 GB). Data-dependent tests skip on main.
- Possible improvement: record real chainsaw + venue/forest background clips and retrain (`uv run python -m training.train --val`, then `--bias data/cache/class_bias.json`).

## Next, use Sonnet subagents
1. **Phase 6 eval** (PLAN.md): replay night1/night2, rules baseline (`agent/baseline.py`) vs commander; `eval/report.md` with alerts, misses, false dispatches; metrics panel in UI. Run on mock, then Ollama.
2. **Phase 7 polish**: LangSmith trace (needs `LANGSMITH_API_KEY` from user), visual pass, outside-in security check, rehearse SPEC §6 demo script, record backup video.
3. Re-run `/graphify --update` after merges.

## Known issues
- `tests/...test_runner_in_real_time_at_60x` is timing-sensitive; flaked once under load.
- `/ws` allows only localhost origins, so the UI can't be opened from a phone over LAN.
- `graphify-out/` and `working-with-me.md` are untracked on main (ask user whether to commit or ignore).

## Standing rules (also in memory)
- Never push without an explicit yes. No AI attribution in commits.
- Cheapest fitting subagent model (haiku/sonnet); opus only for hard work.
- ChatOverflow: search on errors, vote on used notes, save a note for every distinct real insight.
- No paid API calls without the user's OK (Haiku mode costs money).
