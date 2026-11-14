# Resume Here

Read this first in any new session. Then read working-with-me.md, PLAN.md and SPEC.md as needed.
Last updated: 2026-11-14.

## State
- **main** has the pipeline, incident commander, simulator + UI, and integration merged. 327 tests pass (`make test`), lint clean. End-to-end night runs (`make test-e2e`, mock LLM). Nothing pushed.
- Agent verified on local Ollama `qwen3:8b` (`make test-ollama` passes, ~2 min).
- Demo: `make dev` → http://127.0.0.1:8000 → Night 1, 60×, play. Screenshot: docs/screenshots/integrated.png.
- Uses a stand-in classifier (`node/fallback_classifier.py`) until `models/classifier.tflite` lands; `pick_classifier()` switches automatically.

## In flight: classifier (Phase 1)
- Worktree: `.claude/worktrees/agent-a9fde28df4a63e0b2`, branch `worktree-agent-a9fde28df4a63e0b2`. **Nothing committed yet**: `node/classifier.py`, `tests/test_classifier.py`, `training/` are untracked there.
- Datasets downloaded in that worktree's `data/` (ESC-50, UrbanSound8K, cache; ~9 GB, git-ignored).
- Best so far (held-out): chainsaw recall ~0.65–0.73 (target ≥0.90), background false alarms ~6–7% (target ≤5%), gunshot 1.00, vehicle ~0.72. int8 model 56 KB (limit 100 KB).
- If the agent is gone, resume it on a cheap model:
  1. In that worktree: `uv sync --group train`; run `uv run python -m training.train --help` to see options.
  2. Train the final model, export `models/classifier.tflite`, write `models/eval.md` with honest numbers.
  3. `make test` + `make lint` there, commit on the branch, then merge into main and run `make test` + `make test-e2e`.
  4. If chainsaw stays well under 0.9: add chainsaw clips from another free dataset or stronger noise mixing; don't keep tuning blindly. Check that a sustained chainsaw still triggers (3 of 5 windows).

## Next (after classifier merge), use Sonnet subagents
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
