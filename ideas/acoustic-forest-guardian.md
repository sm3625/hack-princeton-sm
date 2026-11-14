# Idea: Acoustic Forest Guardian (edge AI against illegal logging and poaching)

## Pitch
A small offline device listens to the forest, recognizes chainsaws, gunshots and trucks on-device, and sends a tiny alert to rangers over LoRa radio. The twist that sets it apart from existing systems: every detection is **signed on the device** so it can serve as tamper-proof evidence, and an LLM at base turns detections into a case file for prosecution.

## Why edge AI (not cloud)
- **No connectivity:** no cell signal deep in the forest.
- **Bandwidth:** continuous audio can't go over LoRa (~50-byte packets); a 20-byte alert can.
- **Battery:** the radio costs far more power than the processor. Classifying locally and sending only alerts means months on battery instead of days.
- **Latency:** rangers need to arrive while loggers are still there.
- **Scale:** many $10–20 nodes, no per-stream cloud bill.

## Prior art (it already exists, so differentiate)
- **Rainforest Connection:** solar "Guardian" devices made from old phones; alerts on chainsaws and gunshots. Unclear whether detection is on-device.
- **TerraSono** (Hackster, open source): ESP32-S3 + INMP441 mic + SX1262 LoRa, TinyML for chainsaw, gunshot and vehicle. Nearly identical to the basic pitch.
- **Forest Guard** (Edge Impulse blog / WILDLABS): solar LoRa mesh, on-device gunshot model.
- **Lumber Guardian** (Byte Lab, commercial): LoRaWAN chainsaw detector, vendor-claimed >95% accuracy.
- **AudioMoth:** open-source $43 recorder; published gunshot detection up to ~500 m in rainforest.
- Academic chainsaw classifiers report >99% accuracy.

Plain "detect chainsaws on-device + LoRa alert" is well-trodden.

## Differentiators
1. **Evidence that holds up in court (main angle).** Each node signs every detection on-device: timestamp, GPS location, hash of the audio clip, model version, so records can't be faked or edited later. An LLM at base assembles a case file from the records. Possible fit for the HackPrinceton **Arlequin "AI-Driven Forensics"** track (seen in an unverified ChatOverflow note; check the official track list).
2. **Catching what chainsaw detectors miss.** Loggers who know about the sensors switch to axes and machetes or log at night. Detect rhythmic manual chopping, trucks and falling trees.
3. **Locating the sound across nodes (stretch).** Pinpoint the source from time differences between 3+ nodes. Needs precise time sync (e.g. GPS time pulse); likely too much for a hackathon.
4. **Biodiversity as a bonus.** The same mic can count bird and frog calls, a standard measure of forest health.

## Architecture
```
[Node: mic → int8 TinyML classifier → sign(event) ] ──LoRa (~50 B)──► [Ranger base station]
                                                                         ├─ verify signature
                                                                         ├─ event log / map
                                                                         └─ LLM → case file / report
```

Signed event record (fits one LoRa packet):
```
node_id | timestamp | lat,lon | class | confidence | sha256(audio clip) prefix | model_ver | signature
```

## Tiered architecture: edge AI + deep agent
The heavy agent never runs on the nodes. Each tier filters for the next.

| Tier | Where | What runs | How often | Compute |
|---|---|---|---|---|
| **1. Sensor node** | Strapped to a tree | int8 TinyML sound classifier, <100 KB | Always, ~every second | Milliwatts, $10 chip |
| **2. Ranger station** | Laptop / small box, offline | deepagents + local LLM via Ollama | Only when an event arrives (a few times a night) | Laptop, minutes |
| **3. Cloud** (optional) | Over satellite link when available | Large model for final case files / reports | Daily or per case | Pay per use |

A node hears 86,400 seconds of audio a day and sends ~3 alerts, so the agent runs ~3 times, not 86,400. The expensive reasoning touches ~0.003% of the data.

### Ranger dispatch agent (tier 2, built with LangChain deepagents)
Possible fit for the HackPrinceton LangChain "Best Use of deepagents" track (seen in an unverified ChatOverflow note; check the official track list).

```
Simulated forest (2D map, browser)
  ├─ sensor nodes ──signed detections──►  DEEP AGENT (incident commander)
  └─ simulated drone  ◄──flight plans──      ├─ triage sub-agent: real threat or false positive?
                                             ├─ drone-pilot sub-agent: plan flight, capture "footage"
                                             ├─ evidence sub-agent: verify signatures, build case file
                                             └─ human approval: ranger approves before dispatch
```

Why each deepagents feature is needed:
- **Long tasks:** an incident runs all night (detections, drone flight, follow-up, report).
- **Sub-agents:** triage, flight planning and evidence each get their own focused context.
- **Filesystem:** one folder per incident: `/incidents/0412/detections.jsonl`, `drone_log.md`, `case_file.md`.
- **Human approval:** dispatching armed rangers toward possible poachers must be approved by a person.
- **Skills:** load the "suspected logging" or "gunshot near herd" procedure only when needed.
- **Memory:** learns patterns like "node 7 false-alarms when it rains" to triage better.

### What deepagents actually adds (be honest with judges)
- **Real problem it solves: alert overload.** 50 nodes produce dozens of detections a night (real chainsaws, rain, branches, one truck heard by 4 nodes). 3 rangers can't chase them all; a raw alert feed gets ignored.
- **What it does:** groups related detections into one incident; judges each with context (node history, weather, time, nearby incidents, follow-up sounds); decides the next step (ignore / watch / drone / ask a person to dispatch); writes a usable case file.
- **What it doesn't do:** detection accuracy (classifier), battery/range/cost (hardware), tamper-proof records (cryptography in code).
- **Not the main differentiator.** Those remain signed evidence and catching manual logging. A judge may ask "couldn't rules + one LLM call do this?" For simple cases, mostly yes. The agent earns its place where the workflow varies: incidents over hours, drone follow-up, case file built step by step, memory across nights.
- **Prove it with a baseline.** Replay a night of 40 detections (2 real):
  - Baseline (rules + SMS for every detection >80% confidence): 40 alerts.
  - Agent: 2 incidents raised for approval, both real, with case files; 38 dismissed with reasons.
  - Slide: "ranger alerts down 95%, no real incidents missed." If the agent doesn't beat the baseline, drop it or keep it only for case files.
- If not entering the LangChain track: rules for grouping/thresholds + one LLM call for the case file gets ~80% of the value.

### Incident commander: the main agent in detail (LangChain track)
**Job:** take in signed detections, decide which are real, coordinate the response, and leave behind a case file a prosecutor can use. It never classifies audio itself and never dispatches anyone without a person's approval.

**When it runs** (not a constant chat loop; each wake-up resumes that incident's saved thread):
- a new detection arrives over LoRa
- a timer it set fires ("re-check sector C4 in 10 min")
- a ranger replies (approval, field report, question)

**Its own tools** (plain code):
| Tool | What it does |
|---|---|
| `get_detections(sector, window)` | Recent verified detections in an area |
| `get_node_info(node_id)` | Node location, battery, false-alarm history |
| `get_weather(sector)` | Rain and wind, the main false-alarm source |
| `schedule_check(incident_id, minutes)` | Wake itself up later |
| `dispatch_rangers(sector, note)` | **Requires a person's approval** |

**Sub-agents** (separate context, 2–3 tools each):
| Sub-agent | Job | Tools |
|---|---|---|
| **triage** | "Is this real?" Groups detections, weighs node history and weather, returns verdict + confidence | `get_detections`, `get_node_info`, `get_weather` |
| **drone pilot** | Plans a flight to the estimated location within battery and no-fly limits, flies it in the sim, reports what the "camera" sees | `plan_route`, `fly_drone`, `get_drone_status` |
| **evidence** | Verifies every signature, builds the timeline, writes the case file | `verify_signature`, `get_detections`, file tools |

**Files:**
```
/incidents/2026-11-14_C4/
   plan.md            ← its own task plan, updated as it goes
   detections.jsonl   ← every signed record, verified
   triage.md          ← verdict + reasoning
   drone_log.md       ← flight plan, observations
   case_file.md       ← final evidence document
/memories/
   nodes.md           ← "node 7: false alarms in heavy rain"
   sectors.md         ← "C4: 3 incidents this month, logging road nearby"
```

**Walkthrough of one night:**
- **02:14** Node 12: chainsaw, 91%. Agent opens an incident folder, writes `plan.md` (triage, then follow up if real).
- **02:14** → **triage**: nodes 11 and 13 also heard chainsaw-like sounds within 40 s; no rain; node 12 has no false-alarm history. Verdict: likely real, 85%, located between the three nodes.
- **02:16** → **drone pilot**: 1.8 km flight, 40% battery (enough to return). Drone flies on the map; sim camera finds "fresh clearing, one vehicle."
- **02:21** Agent calls `dispatch_rangers("C4", ...)` and **stops for a person**: "Likely illegal logging in sector C4. 3 nodes agree, drone confirms a vehicle and fresh clearing. Dispatch team?" [Approve] [Edit] [Reject]
- **02:22** Ranger approves. Agent schedules a check in 30 min.
- **02:52** Node 12: vehicle, moving away. Logged: suspects probably leaving.
- **06:00** → **evidence**: verifies all 9 signatures, writes `case_file.md` (timeline, node locations, audio hashes, drone observations, the approval record, statement that every record was checked as unaltered). Memory updated: "C4: second incident this month."
- **Same night:** node 7 sent 6 alerts in a rainstorm. Triage checked weather + node history and dismissed them with reasons. No ranger disturbed.

**Code vs LLM:**
| Plain code (exact, reliable) | LLM (judgment and writing) |
|---|---|
| Signature checks | Is this real, given context? |
| Locating the source from node positions | Which follow-up to try next |
| Drone route and battery math | When to involve a person |
| Timers, storage | Writing the case file and explanations |

**Setup sketch** (approximate; check against deepagents docs):
```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="anthropic:claude-sonnet-5-5",   # or a local Ollama model
    system_prompt=COMMANDER_PROMPT,
    tools=[get_detections, get_node_info, get_weather,
           schedule_check, dispatch_rangers],
    subagents=[
        {"name": "triage", "description": "Decide if detections are a real threat",
         "system_prompt": TRIAGE_PROMPT,
         "tools": [get_detections, get_node_info, get_weather]},
        {"name": "drone-pilot", "description": "Plan and fly drone checks",
         "system_prompt": PILOT_PROMPT,
         "tools": [plan_route, fly_drone, get_drone_status]},
        {"name": "evidence", "description": "Verify records, write case files",
         "system_prompt": EVIDENCE_PROMPT,
         "tools": [verify_signature, get_detections]},
    ],
    interrupt_on={"dispatch_rangers": True},   # human approval
    # + a checkpointer so each incident resumes, and a filesystem backend
    #   so /incidents and /memories persist on disk
)
```

**What to show LangChain judges:**
1. Replay a night: 40 detections in, 2 incidents raised for approval, 38 dismissed with reasons.
2. The agent's `plan.md` updating live as the incident unfolds.
3. Sub-agents handing work back and forth in a LangSmith trace.
4. The approval step stopping a dispatch until a person approves.
5. Memory changing behavior: on night two, node 7's rain alarms are dismissed faster.

### Compute reality
- The deepagents framework itself is light Python; the LLM is the cost. Long system prompt + many tool definitions + sub-agents → ~10–30 LLM calls per incident.
- Offline, an 8B tool-calling model (Qwen / Llama family) on a laptop via Ollama: maybe tens of seconds per step. Fine for an incident that unfolds over ~20 minutes.
- **Caveat:** small local models are noticeably worse at long multi-step tool use (lose the plan, wrong tool, bad arguments). Mitigations:
  - each sub-agent gets only 2–3 tools and a short prompt
  - exact work (signature checks, distances, drone paths) in ordinary code; the LLM only decides and writes
  - fallback: cloud model when the satellite link is up, local model otherwise
- For the demo, run the station on a cloud model for reliability, and show a clip of the same agent running on a local 8B model offline.

### Simulating the physical parts
- **Forest + sensors:** 2D grid in the browser. Scripted events play real chainsaw/gunshot clips through the real classifier, so detection is real and only location is simulated.
- **Drone:** 2D drone with battery, speed and no-fly zones; or PyBullet / MuJoCo (pip-installable, laptop) for 3D.

### deepagents gotchas (from another agent's ChatOverflow notes; not yet tested)
- Files live in agent state by default, not on disk: read `result['files']` or configure a filesystem backend.
- `write_todos` isn't built in: add `TodoListMiddleware()` from `langchain.agents.middleware`.
- A sub-agent's `tools` list **replaces** the main agent's tools instead of adding to them.

### Pitch line
> "A tiny always-on model in the field and a large reasoning model only when it matters. The forest node runs on milliwatts and sends 3 alerts a night instead of streaming 24 hours of audio. The ranger station runs an agent on a laptop, offline, only when something happens."

## Model
- Train in Edge Impulse or TensorFlow; export an int8 TFLite Micro model (<100 KB).
- Data: ESC-50 and UrbanSound8K (include chainsaw and gunshot classes), plus a "background forest" class (birds, rain, wind) to keep false positives down.
- Classes: chainsaw, gunshot, vehicle/truck, (stretch) axe chopping, background.

## Hardware (no Raspberry Pi needed)
- **Target:** ESP32-S3 + I2S mic (INMP441) + LoRa (SX1262), or Arduino Nano 33 BLE Sense (built-in mic). An Uno can't run the model.
- **Without hardware:**
  1. **Laptop** runs the same int8 model on the built-in mic, with device limits enforced in code: model <100 KB, memory <256 KB, events capped at LoRa packet size, "radio" = local socket to a base-station process.
  2. **Phone in airplane mode** running the TFLite model: a real offline edge device, good for the live demo.
  3. **Renode** (renode.io) emulates the Nano 33 BLE's Cortex-M4 to prove the firmware fits the real chip.
  4. Wokwi only for non-audio firmware logic (it doesn't simulate mic input properly).
- Ask the HackPrinceton hardware desk for an ESP32-S3 or Nano 33 BLE Sense.

## Demo plan
1. Phone in airplane mode (or board), listening.
2. Play a chainsaw clip: detection fires and a signed record is sent to the base station, which verifies it and shows it on a map.
3. Play birdsong: no alert.
4. Edit one byte of a stored record: verification fails, showing tamper-evidence.
5. Ranger dispatch agent triages the detection, plans a drone flight (drone flies on the map), asks "Dispatch rangers to sector C4?"; approve, and it writes the case file.

## Sources
- LangChain deepagents: https://github.com/langchain-ai/deepagents
- TerraSono: https://hackster.io/rishabh-jain5/terrasono-a-sustainable-acoustic-intelligence-system-f56d2f
- Edge Impulse, LoRa + AI forests: https://edgeimpulse.com/blog/how-lora-and-ai-are-protecting-our-forests/
- Forest Guard (WILDLABS): https://wildlabs.net/en/comment/13693
- Byte Lab Lumber Guardian: https://www.byte-lab.com/projects/smart-forest-protection-system
- Tiny AI devices listening for poachers: https://tech.yahoo.com/science/articles/tiny-ai-devices-listening-poachers-133200251.html
- Rainforest Connection (Gizmodo): https://gizmodo.com/this-nonprofit-is-using-gunshot-detection-tech-to-fight-1823997516
- AudioMoth, Methods Ecol. Evol. 2018: https://media.rufford.org/media/project_reports/17047-1Methods%20Ecol.%20Evol.%202018%2C%201-13.pdf
- AudioMoth gunshot detection, Sensors 2019: https://eprints.soton.ac.uk/428582/1/sensors_19_00553.pdf
- Chainsaw-sound recognition (Huddersfield): https://pure.hud.ac.uk/en/publications/a-chainsaw-sound-recognition-model-for-detecting-illegal-logging-/
- Results in Engineering, Dec 2024: https://doaj.org/article/d74d6028e55142fa8ea09bafb6c2112a
- LoRa sound-classification alert system: https://sensors.myu-group.co.jp/sm_pdf/SM4231.pdf
