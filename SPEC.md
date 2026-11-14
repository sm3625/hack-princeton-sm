# Acoustic Forest Guardian: Technical Spec

Status: draft v0.1 (2026-11-14). Background, prior art and pitch: [ideas/acoustic-forest-guardian.md](ideas/acoustic-forest-guardian.md).

## 1. Summary
Offline sensor nodes classify forest sounds on-device and send small **signed** detection records to a ranger station. At the station, a deepagents **incident commander** groups detections into incidents, triages them, sends a simulated drone to check, asks a ranger before any dispatch, and writes a tamper-evident case file. Everything runs on a laptop; hardware is optional.

## 2. Goals and non-goals
**Goals (demo-critical)**
- G1. Real on-device-style classification: int8 TFLite model, <100 KB, run on live mic audio.
- G2. Every detection signed on the node; station rejects tampered records.
- G3. Incident commander built with deepagents: sub-agents, files, human approval, memory.
- G4. Simulated forest map with nodes and a drone, visible live.
- G5. Measured comparison: agent vs. simple rules baseline on a replayed night.

**Stretch**
- S1. Axe-chopping class (manual logging).
- S2. Firmware running on Renode (emulated Cortex-M4) or a borrowed Nano 33 BLE Sense / ESP32-S3.
- S3. Same agent on a local 8B model via Ollama.

**Non-goals**
- Real LoRa radios, real drones, field accuracy claims.
- Multi-node time-difference localization (needs precise time sync). Location is estimated from which nodes heard it.

## 3. Components
```
 ┌──────────── node (×N, simulated) ────────────┐      ┌──────────── ranger station ────────────┐
 │ mic/clip → features → int8 classifier        │      │ receiver → verify sig → event store    │
 │        → debounce → record → Ed25519 sign    │─UDP─►│        → wake incident commander       │
 │        → keep clip on local "SD"             │ (LoRa│ deepagents: triage / drone / evidence  │
 └──────────────────────────────────────────────┘  sim)│ files: /incidents, /memories           │
                                                       └───────────────┬────────────────────────┘
                     web UI: map · alert feed · approval card · case files · metrics ◄─┘ (WebSocket)
```

### 3.1 Classifier
- **Classes:** `chainsaw`, `gunshot`, `vehicle`, `background` (birds, rain, wind, insects). Stretch: `axe`.
- **Data:** ESC-50 (chainsaw, engine, rain, birds, wind, insects …), UrbanSound8K (gun_shot, engine_idling), plus self-recorded background. Keep per-source train/test split to avoid leakage.
- **Input:** 16 kHz mono, 1 s windows, 50% hop, MFCC or log-mel features.
- **Training:** Edge Impulse (fastest) or Keras small CNN → int8 TFLite.
- **Targets:** model ≤100 KB, inference ≤20 ms on laptop; chainsaw recall ≥90% and background false-positive rate ≤5% on held-out clips.
- **Output per window:** class + confidence.

### 3.2 Node runtime (`node/`)
Simulated nodes run on the laptop. One real node uses the live mic; others replay scenario clips.
- **Debounce:** emit an event when the same non-background class is ≥0.8 confidence in ≥3 of the last 5 windows; then suppress that class for 30 s. Gunshots are impulses (one shot spans at most 2 windows), so for `gunshot` the rule is ≥2 of 5.
- **Clip storage:** save the 5 s of audio around the event to `node_data/<node_id>/clips/` (stands in for SD card, collected later as evidence).
- **Device limits enforced in code:** model size, max record size, events/hour budget. Log and display them.
- **Keys:** each node has an Ed25519 keypair generated at provisioning; station holds the public keys.

### 3.3 Detection record (wire format)
Binary, little-endian, 108 bytes total:

| Field | Bytes | Notes |
|---|---|---|
| version | 1 | `0x01` |
| node_id | 2 | |
| seq | 4 | per-node counter; detects gaps and replays |
| timestamp | 4 | unix seconds |
| lat, lon | 8 | int32 each, degrees × 1e7 |
| class | 1 | enum |
| confidence | 1 | 0–255 |
| model_ver | 2 | |
| clip_hash | 16 | first 16 bytes of SHA-256 of the stored clip |
| reserved | 5 | |
| signature | 64 | Ed25519 over the 44 bytes above |

**Size note:** this exceeds the "~50 bytes" rule of thumb used in the idea doc. Real LoRa allows ~51–222 bytes depending on data rate and region, so 108 bytes needs a mid-to-fast data rate. Alternatives considered:
- HMAC (16 bytes) fits easily, but the station holds the key and could forge records, which weakens the evidence claim. **Rejected.**
- Send the signature in a second packet. Possible later if needed.

### 3.4 Transport sim (`transport/`)
- UDP from node processes to the station.
- Configurable packet loss (default 5%) and latency (100–2000 ms); hard-reject payloads >222 bytes.
- No retries in v0.1; `seq` gaps are shown in the UI.

### 3.5 Station receiver and event store (`station/`)
- Python, FastAPI + WebSocket to the UI.
- On receive: parse → verify signature with the node's public key → check `seq` is new → store in SQLite (`events` table: raw bytes, parsed fields, verified flag, received_at).
- Invalid signature: store with `verified=false`, show red in UI, **never** passed to the agent as evidence.
- Wakes the commander for each verified event (or batches events arriving within 10 s).

### 3.6 Incident commander (`agent/`)
Built with `deepagents.create_deep_agent`. Full design rationale is in the idea doc; contract here.

- **Model:** Claude Sonnet (demo); Ollama 8B tool-calling model (stretch S3).
- **Threads:** one LangGraph thread per incident, saved with a SQLite checkpointer so each wake-up resumes.
- **Triggers:** new verified event · scheduled check · ranger message.
- **Main tools:** `get_detections(sector, window_s)`, `get_node_info(node_id)`, `get_weather(sector)`, `schedule_check(incident_id, minutes)`, `dispatch_rangers(sector, note)` (human approval via `interrupt_on`).
- **Sub-agents:**
  - `triage`: tools `get_detections`, `get_node_info`, `get_weather` → returns `{verdict: real|false_alarm|uncertain, confidence, location_estimate, reasons[]}`.
  - `drone-pilot`: tools `plan_route`, `fly_drone`, `get_drone_status` → returns observation summary.
  - `evidence`: tools `verify_signature`, `get_detections`, file tools → writes `case_file.md`.
- **Files** (filesystem backend on disk, rooted at `agent_fs/`):
  - `/incidents/<date>_<sector>/{plan.md, detections.jsonl, triage.md, drone_log.md, case_file.md}`
  - `/memories/{nodes.md, sectors.md}`
- **Rules in the system prompt:** never dispatch without approval; never treat unverified records as evidence; cite record `seq` numbers for every claim in a case file.
- **Known gotchas (from other agents' notes, verify):** files default to agent state, not disk (configure backend); `write_todos` needs `TodoListMiddleware`; sub-agent `tools` replaces inherited tools.

### 3.7 Simulator (`sim/`)
- 2D forest grid (e.g. 4×4 km, 500 m sectors A1–H8), ~12 nodes with positions.
- **Scenario files** (`scenarios/*.yaml`): timed events, each mapping to a node and an audio clip, plus weather changes. Clips are run through the real classifier, so detections are genuine.
- **Weather:** rain periods raise background noise and trigger false alarms on chosen "noisy" nodes.
- **Drone:** position, speed 10 m/s, battery %, no-fly zones; `fly_drone` animates it on the map and returns a scripted observation for the target cell (e.g. "fresh clearing, one vehicle"). Time can run at 10–60× speed.

### 3.8 Web UI (`ui/`)
Single page, plain HTML + JS (or React if time), canvas or Leaflet for the map.
- Map: nodes (green / amber on detection / red on bad signature), sectors, drone path.
- Live alert feed: every detection, verified or not.
- Incident panel: current `plan.md`, sub-agent activity.
- **Approval card:** Approve / Edit / Reject for `dispatch_rangers`.
- Case file viewer (rendered markdown).
- Metrics panel: alerts sent (baseline) vs. incidents raised (agent).
- Tamper demo button: flip one byte in a stored record and re-verify.

### 3.9 Evaluation (`eval/`)
- **Scenario `night1.yaml`:** ~40 detections over 8 sim-hours, 2 real incidents, the rest rain false alarms, a truck heard by 4 nodes, and a branch fall.
- **Baseline:** SMS-equivalent alert for every verified detection with confidence ≥0.8.
- **Agent:** count incidents raised for approval, real incidents missed, false dispatches.
- **Report:** table + one slide: alerts to rangers (baseline vs. agent), recall on real incidents.
- **Night 2** (`night2.yaml`): same noisy nodes; check that memory reduces triage steps or time.
- Classifier metrics from 3.1 reported separately.

## 4. Tech stack
Python 3.11+, `uv` · TFLite runtime / `ai-edge-litert` · `sounddevice` (mic) · `librosa` (features) · `pynacl` (Ed25519) · FastAPI + WebSocket · SQLite · `deepagents`, LangGraph checkpointer, LangSmith tracing · Ollama (stretch) · plain HTML/JS UI.

## 5. Repo layout
```
node/        classifier runtime, debounce, signing, clip storage
transport/   UDP LoRa sim
station/     receiver, verification, event store, API
agent/       incident commander, sub-agents, tools, prompts
sim/         world model, drone, scenario runner
scenarios/   *.yaml + audio clips
ui/          web front end
eval/        baseline, replay, metrics report
models/      trained .tflite + training notebook
ideas/       idea docs
```

## 6. Demo script (3 min)
1. Map up; nodes listening. Show the model's size and speed on screen.
2. Play a chainsaw clip at the laptop mic → node amber → signed record verified at the station.
3. Commander opens an incident; `plan.md` updates; triage verdict; drone flies on the map.
4. Approval card appears → approve.
5. Fast-forward → case file appears, every claim citing record numbers.
6. Flip one byte → verification fails, shown red.
7. Metrics slide: night replay, baseline alerts vs. agent incidents.
8. (Stretch) clip of the same agent on a local 8B model, Wi-Fi off.

## 7. Build plan (about 36 h)
| Hours | Milestone |
|---|---|
| 0–4 | Classifier trained and exported; live mic inference working |
| 4–8 | Record format, signing, UDP transport, station verify + store |
| 8–14 | Simulator + map UI with nodes and live feed |
| 14–22 | Incident commander: tools, sub-agents, files, approval |
| 22–26 | Drone sim, case files, tamper demo |
| 26–30 | Scenarios, baseline, eval report |
| 30–34 | Polish, LangSmith trace, stretch goals |
| 34–36 | Rehearse demo, record backup video |

## 8. Risks
| Risk | Mitigation |
|---|---|
| Classifier fires on music/speech in a noisy venue | Include speech/music in `background`; demo with clips played close to the mic; backup: scenario replay |
| Agent slow or flaky live | Cloud model for demo; cap sub-agent tools; recorded backup run |
| deepagents API differs from sketch | Spike a minimal agent with one sub-agent and `interrupt_on` in the first 2 hours of agent work |
| Record too large for real LoRa | Documented in 3.3; second-packet option |
| Scope creep | Stretch goals only after section 7 milestones through hour 30 |

## 9. Open questions
- Which tracks are confirmed (LangChain deepagents, Arlequin forensics)? Check the official list.
- Team size and split (ML / station+agent / UI)?
- Can we borrow a Nano 33 BLE Sense or ESP32-S3 from the hardware desk?
- Scenario location: a real forest's coordinates for realism, or a fictional map?
