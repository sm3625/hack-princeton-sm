# Build Plan: Acoustic Forest Guardian

Builds what [SPEC.md](SPEC.md) describes. Follows [working-with-me.md](working-with-me.md): local first, one command to start, tests for every piece, no paid services without a yes, nothing pushed without a yes.

## Ground rules for this build
- **One command:** `make dev` starts the station, simulator, nodes and UI on localhost. No sign-in locally.
- **Free by default:**
  - Ollama (local, free) is the default model for agent development.
  - One-line switch to Claude Haiku 5.5 (about 3¢ per incident) in `.env`. See "Model switching" below.
  - LangSmith free tier for traces.
  - Edge Impulse free plan, or Keras locally, for training.
  - Larger Claude models only for the demo run, after you OK the cost.
- **Protect quotas:**
  - `LLM_MODE=mock` returns scripted agent replies, so UI and sim work burns no LLM calls.
  - Replays never loop without a cap.
- **Secrets:**
  - `.env` (git-ignored) for API keys; `.env.example` lists names only.
  - Node private keys live in `keys/` (git-ignored); only public keys are committed.
  - Pre-commit hook blocks key-shaped strings and private key files.
- **Replaceable backends**, each behind a small interface:
  - LLM provider (Ollama / Claude / mock)
  - transport (UDP sim / real LoRa later)
  - event store (SQLite)
  - weather (scenario / real API)
  - drone (sim / real)
- **Tests:** `pytest` per module; Playwright for the UI flow. A step is done only when its tests pass and I've driven it in the browser.
- **Git:** commit after each phase. Push only when you say so.

## Model switching
One setting in `.env` picks the model; no code changes.

```bash
LLM_MODE=ollama          # default: local, free
# LLM_MODE=haiku         # Claude Haiku 5.5, ~3¢ per incident, needs ANTHROPIC_API_KEY
# LLM_MODE=mock          # scripted replies, no model, for UI/sim work
OLLAMA_MODEL=qwen3:8b    # any Ollama model with tool calling
```

- `agent/llm.py` maps each mode to a LangChain chat model (`ChatOllama` / `ChatAnthropic` with `claude-haiku-5-5` / a fake model), so deepagents sees the same interface.
- Optional per-sub-agent override later (e.g. `TRIAGE_MODE=haiku`), only if Ollama struggles with one sub-agent.
- Switching to `haiku` without `ANTHROPIC_API_KEY` stops at startup with a clear message.
- The UI shows which model is running, so a demo never runs on the wrong one by accident.
- Same tests run in all three modes: `make test` (mock), `make test-ollama`, `make test-haiku` (opt-in, spends a few cents).

## Phase 0: Skeleton (≈1 h)
- [ ] Repo layout from SPEC §5, `pyproject.toml` with `uv`, `Makefile` (`dev`, `test`, `lint`).
- [ ] `.gitignore` additions (`.env`, `keys/`, `node_data/`, `agent_fs/`, `*.sqlite`), `.env.example`.
- [ ] Pre-commit hook: block API-key patterns and `*.key` / PEM files.
- [ ] Empty FastAPI app + static UI page served at `localhost:8000`.
- **Done when:** `make dev` opens a blank page; `make test` runs (0 tests OK); hook blocks a fake key in a test commit.

## Phase 1: Classifier (≈4 h) · SPEC §3.1
- [ ] Download ESC-50 + UrbanSound8K subsets; build `chainsaw / gunshot / vehicle / background` with source-aware split. Add speech/music to `background` (noisy venue).
- [ ] Train small CNN on log-mel 1 s windows; export int8 `.tflite` to `models/`.
- [ ] `node/classifier.py`: load model, classify a window, report size and ms per inference.
- [ ] Eval script: per-class recall, background false-positive rate.
- **Tests:** known clips classify correctly; model file ≤100 KB; inference ≤20 ms.
- **Done when:** chainsaw recall ≥90%, background FP ≤5% on held-out clips; live mic demo prints classes.

## Phase 2: Node → station pipeline (≈4 h) · SPEC §3.2–3.5
- [ ] `node/record.py`: 108-byte record pack/unpack, Ed25519 sign (pynacl).
- [ ] `node/keys.py`: provision a keypair per node; write public keys to `station/node_keys.json`.
- [ ] `node/runtime.py`: windowing, debounce (3 of 5 ≥0.8, 30 s suppress), clip save + hash, limits logged.
- [ ] `transport/udp.py`: loss, latency, 222-byte cap.
- [ ] `station/receiver.py`: verify signature, reject replayed `seq`, store in SQLite (parameterized SQL), push to UI over WebSocket.
- **Tests:** pack/unpack round-trip; one flipped byte fails verification; replay rejected; oversize packet rejected; debounce fires once per burst.
- **Done when:** a chainsaw clip played at the mic shows up as a verified event in the station log.

## Phase 3: Simulator + map UI (≈6 h) · SPEC §3.7–3.8
- [ ] `sim/world.py`: 4×4 km grid, 500 m sectors, ~12 nodes with **real names** ("Ridge North", "Creek Bend"), never raw IDs on screen.
- [ ] `sim/scenario.py`: YAML scenarios (timed clips → nodes, weather changes); time speed 1–60×.
- [ ] `sim/drone.py`: position, 10 m/s, battery, no-fly zones, scripted observations.
- [ ] UI (calm, Apple-like, few words, works on a phone):
  - map: nodes, sectors, drone path
  - live feed
  - incident panel
  - **approval card**, usable on a phone at the "ranger" end
  - case file viewer: markdown rendered **sanitized**, since it's LLM output
- [ ] Deep links: `/incident/<id>` opens that incident.
- **Tests:** scenario timing; drone battery and no-fly limits; Playwright: map loads, event appears, mobile viewport works.
- **Done when:** `night1.yaml` plays on the map with nodes lighting up, in mock agent mode.

## Phase 4: Incident commander (≈8 h) · SPEC §3.6
- [ ] **Spike first (1 h):** minimal `create_deep_agent` with one sub-agent, `interrupt_on`, SQLite checkpointer, filesystem backend writing to `agent_fs/`. Confirm the gotchas from SPEC §3.6 before building on them.
- [ ] `agent/llm.py`: provider switch from `LLM_MODE`: `ollama` (default), `haiku`, `mock`. See "Model switching".
- [ ] Tools: `get_detections`, `get_node_info`, `get_weather`, `schedule_check`, `dispatch_rangers` (approval).
- [ ] Sub-agents: `triage`, `drone-pilot`, `evidence` (2–3 tools each, short prompts).
- [ ] Wake-ups: new verified event (10 s batching), scheduled check, ranger message; one thread per incident.
- [ ] Memory files: `/memories/nodes.md`, `/memories/sectors.md`.
- [ ] Approval route: the station only acts on an approval for an open interrupt, so the API can't be used to dispatch directly.
- **Tests:**
  - unverified records never reach the agent
  - dispatch blocks until approved
  - reject cancels dispatch
  - case file cites record numbers that exist
  - all run in mock mode, plus one tagged test against Ollama
- **Done when:** the full 02:14→06:00 walkthrough from the idea doc runs end to end on Ollama, and again with `LLM_MODE=haiku` after changing only `.env`.

## Phase 5: Evidence + tamper demo (≈3 h)
- [ ] `evidence` sub-agent writes `case_file.md`: timeline, node names, audio hashes, drone notes, approval record.
- [ ] Station check: recompute clip hashes from `node_data/` and re-verify all signatures when a case file is opened.
- [ ] UI button: flip one byte in a stored record and re-verify (shows red).
- **Tests:** tampered clip or record fails; untouched case verifies.

## Phase 6: Evaluation (≈4 h) · SPEC §3.9
- [ ] `night1.yaml`: ~40 detections, 2 real incidents; `night2.yaml` for memory.
- [ ] Baseline: alert per verified detection ≥0.8.
- [ ] Runner: replay both, count alerts / incidents / misses / false dispatches, write `eval/report.md`.
- [ ] Metrics panel in the UI from the report.
- **Done when:** report shows agent vs baseline with real numbers; if the agent doesn't beat the baseline, flag it before the demo work.

## Phase 7: Polish + demo (≈6 h)
- [ ] LangSmith trace view for the demo incident.
- [ ] Claude run for the demo (after a cost OK); recorded backup video.
- [ ] Visual pass: spacing, motion, crowding on the map at each zoom.
- [ ] Quick outside-in security check: CORS allowlist, escaped output, no keys in repo or bundle.
- [ ] Rehearse the 3-minute script (SPEC §6).

## Stretch (only after Phase 6)
- Axe-chopping class.
- Renode run of the firmware on an emulated Cortex-M4, or a borrowed board.
- Clip of the agent on a local model, Wi-Fi off.

## Things only you can do
- Install Ollama and pull a tool-calling model, e.g. a Qwen or Llama 8B.
- Create a LangSmith account (free) and put the key in `.env` as `LANGSMITH_API_KEY`.
- To use Haiku: create a Claude Console account, add a few dollars of credit, put the key in `.env` as `ANTHROPIC_API_KEY`, and set `LLM_MODE=haiku`.
- Check the official track list (LangChain deepagents, Arlequin forensics).
- Ask the hardware desk for a Nano 33 BLE Sense or ESP32-S3 (optional).
