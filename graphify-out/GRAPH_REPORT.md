# Graph Report - hack-princeton-sm  (2026-11-13)

## Corpus Check
- Corpus is ~9,563 words - fits in a single context window. You may not need a graph.

## Summary
- 234 nodes · 354 edges · 19 communities (9 shown, 10 thin omitted)
- Extraction: 87% EXTRACTED · 13% INFERRED · 0% AMBIGUOUS · INFERRED: 46 edges (avg confidence: 0.91)
- Token cost: 26,000 input · 14,000 output

## Community Hubs (Navigation)
- Acoustic Guardian Idea & Prior Art
- Build Plan & Tooling
- Shared Contracts
- Station Server & Config
- Forest World Map
- Incident Commander Agent
- Secret-Blocking Hook Tests
- Message Bus
- Station Tests
- Ventilation Coach Idea
- UI Script
- Project Manifest

## God Nodes (most connected - your core abstractions)
1. `Incident commander (deepagents create_deep_agent)` - 20 edges
2. `Acoustic Forest Guardian Technical Spec` - 17 edges
3. `Idea: Acoustic Forest Guardian (edge AI against illegal logging and poaching)` - 16 edges
4. `Working With Me (agent collaboration guidelines)` - 15 edges
5. `Build Plan: Acoustic Forest Guardian` - 13 edges
6. `nodes` - 8 edges
7. `Phase 3: Simulator + map UI` - 8 edges
8. `Sound classifier (int8 TFLite: chainsaw / gunshot / vehicle / background)` - 8 edges
9. `Detection record wire format (108 bytes)` - 8 edges
10. `Station receiver and SQLite event store` - 8 edges

## Surprising Connections (you probably didn't know these)
- `Things only you can do (manual setup steps)` --references--> `HackPrinceton LangChain Best Use of deepagents track`  [INFERRED]
  PLAN.md → ideas/acoustic-forest-guardian.md
- `#status connection indicator` --shares_data_with--> `Station receiver and SQLite event store`  [INFERRED]
  ui/index.html → SPEC.md
- `test_haiku_without_key_fails_clearly()` --uses--> `ConfigError`  [INFERRED]
  tests/test_station.py → station/config.py
- `test_unknown_mode_rejected()` --uses--> `ConfigError`  [INFERRED]
  tests/test_station.py → station/config.py
- `Code Cleanup Skill (remove AI slop)` --conceptually_related_to--> `Repo layout (node/, transport/, station/, agent/, sim/, ui/, eval/)`  [AMBIGUOUS]
  .claude/skills/code-cleanup/SKILL.md → SPEC.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Signed detection pipeline: node classifier -> signed record -> LoRa sim -> station -> commander** — spec_node_runtime, spec_classifier, spec_detection_record, spec_transport_sim, spec_station_receiver, spec_incident_commander [EXTRACTED 1.00]
- **Incident response flow: triage, drone check, human-approved dispatch, case file** — spec_triage_subagent, spec_drone_pilot_subagent, spec_dispatch_rangers, spec_approval_card, spec_evidence_subagent, spec_case_file, ideas_acoustic_forest_guardian_night_walkthrough [EXTRACTED 1.00]
- **Tamper-evidence chain: on-node signing through verified case file** — ideas_acoustic_forest_guardian_signed_evidence, spec_ed25519_signing, spec_detection_record, spec_evidence_subagent, spec_case_file, spec_tamper_demo, plan_phase_5_evidence_tamper_demo [INFERRED 0.85]

## Communities (19 total, 10 thin omitted)

### Community 0 - "Acoustic Guardian Idea & Prior Art"
Cohesion: 0.08
Nodes (40): Idea: Acoustic Forest Guardian (edge AI against illegal logging and poaching), HackPrinceton Arlequin AI-Driven Forensics track, AudioMoth acoustic recorder, Biodiversity call counting (bonus), Target hardware: ESP32-S3 + INMP441 + SX1262 / Nano 33 BLE Sense, Forest Guard (Edge Impulse / WILDLABS), LoRa radio link, Lumber Guardian (Byte Lab) (+32 more)

### Community 1 - "Build Plan & Tooling"
Cohesion: 0.07
Nodes (21): Code Cleanup Skill (remove AI slop), Backend Commenting Standard (docs/backend-commenting.md), Drizzle migration artifacts to revert (_journal.json, snapshots), Global MutationCache.onError handler, useOptimism hook (optimistic cache updates), Build Plan: Acoustic Forest Guardian, agent/llm.py LLM provider switch, Claude Haiku 5.5 option (about 3 cents per incident) (+13 more)

### Community 2 - "Shared Contracts"
Cohesion: 0.07
Nodes (12): Classifier, Detection, DroneStatus, EventStore, load_world(), Node, Prediction, Route (+4 more)

### Community 3 - "Station Server & Config"
Cohesion: 0.12
Nodes (5): create_app(), ConfigError, load_settings(), Settings, ws()

### Community 4 - "Forest World Map"
Cohesion: 0.12
Nodes (16): base_x_m, base_y_m, height_m, no_fly, nodes, id, lat, lon (+8 more)

### Community 5 - "Incident Commander Agent"
Cohesion: 0.20
Nodes (14): LangChain deepagents (create_deep_agent), HackPrinceton LangChain Best Use of deepagents track, One-night walkthrough (02:14 to 06:00, sector C4), Phase 4: Incident commander, Known deepagents gotchas (files in state not disk, TodoListMiddleware, sub-agent tools replace), dispatch_rangers(sector, note) tool (interrupt_on approval), drone-pilot sub-agent, Simulated drone (10 m/s, battery, no-fly zones) (+6 more)

### Community 6 - "Secret-Blocking Hook Tests"
Cohesion: 0.24
Nodes (8): commit(), repo(), git(), test_allows_env_example_and_normal_code(), test_blocks_anthropic_key(), test_blocks_env_file(), test_blocks_node_keys(), test_blocks_private_key()

### Community 8 - "Station Tests"
Cohesion: 0.38
Nodes (6): make_client(), test_haiku_without_key_fails_clearly(), test_health_reports_model(), test_index_serves_ui(), test_unknown_mode_rejected(), test_ws_forwards_bus_messages()

### Community 9 - "Ventilation Coach Idea"
Cohesion: 0.24
Nodes (8): Idea: AI Ventilation Coach (Arduino air-quality sensor), Calibration against AirNow / OpenAQ (simpler alternative), Curve-shape event classifier (spray / cooking / poor ventilation), Ventilation coach LLM agent with tools (fit_decay, forecast, analyze_room_photo, set_fan, log_experiment), MQ-135 Arduino air-quality sensor, Time-series foundation model (Chronos / TimesFM / Moirai), Vision LLM room-photo analysis, Voice interface (Whisper + TTS)

## Ambiguous Edges - Review These
- `Code Cleanup Skill (remove AI slop)` → `Repo layout (node/, transport/, station/, agent/, sim/, ui/, eval/)`  [AMBIGUOUS]
  .claude/skills/code-cleanup/SKILL.md · relation: conceptually_related_to

## Knowledge Gaps
- **19 isolated node(s):** `forest-guardian`, `Backend Commenting Standard (docs/backend-commenting.md)`, `Drizzle migration artifacts to revert (_journal.json, snapshots)`, `useOptimism hook (optimistic cache updates)`, `Global MutationCache.onError handler` (+14 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 79 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **10 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Acoustic Forest Guardian Technical Spec` connect `Acoustic Guardian Idea & Prior Art` to `Build Plan & Tooling`, `Incident Commander Agent`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Are the 6 inferred relationships involving `Incident commander (deepagents create_deep_agent)` (e.g. with `Alert overload (problem the agent solves)` and `Code vs LLM split (exact work in code, judgment in the LLM)`) actually correct?**
  _`Incident commander (deepagents create_deep_agent)` has 6 INFERRED edges - model-reasoned connections that need verification._
- **What connects `forest-guardian`, `Backend Commenting Standard (docs/backend-commenting.md)`, `Drizzle migration artifacts to revert (_journal.json, snapshots)` to the rest of the system?**
  _19 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Acoustic Guardian Idea & Prior Art` be split into smaller, more focused modules?**
  _Cohesion score 0.0824524312896406 - nodes in this community are weakly interconnected._
- **Why does `Incident commander (deepagents create_deep_agent)` connect `Incident Commander Agent` to `Acoustic Guardian Idea & Prior Art`, `Ventilation Coach Idea`, `Build Plan & Tooling`?**
  _High betweenness centrality (0.066) - this node is a cross-community bridge._
- **Should `Build Plan & Tooling` be split into smaller, more focused modules?**
  _Cohesion score 0.07051282051282051 - nodes in this community are weakly interconnected._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Code Cleanup Skill (remove AI slop)` and `Repo layout (node/, transport/, station/, agent/, sim/, ui/, eval/)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._