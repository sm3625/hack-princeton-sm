# What to hand a frontier coding model (Fable 5.2)

Written 2026-11-14. Rule of thumb: give the strongest model the problems that are hard and
expensive to get wrong; leave routine work to Sonnet or Haiku.

## Best uses, in order of value

### 1. Fix the classifier's false alarms (strongest case)
Background noise triggers 6.9% of windows (target 5%), and chainsaw precision is about 8%: air
conditioners, insects and wind get called "chainsaw". Everything downstream inherits this.
- Error analysis on the confused classes; add hard-negative training data.
- Calibrate per-class thresholds.
- Per-node adaptive thresholds, so a noisy node learns its own baseline.
- Keep the model under 100 KB as int8.

Real ML iteration with many judgment calls, where a strong model saves days.
See `models/eval.md`, `training/`.

### 2. Make it hold up at normal speed and in storms
From `docs/SCALING_NOTES.md`:
- a code pre-filter that clears obvious rain alarms without the model
- a priority queue so threats jump ahead
- an immediate heads-up alert, so the queue never delays the first warning
- slimmer prompts, to cut the 30–45 s per incident

Then prove it with a full-night replay. This touches the concurrency-sensitive commander loop,
where subtle bugs like the earlier pause/drone deadlock live.

### 3. A trustworthy eval
The pitch's numbers depend on it.
- **Deterministic replay:** the clock steps in lockstep with the agent, so runs repeat exactly.
- **A fair scoring rule:** today an approved request counts as a catch; tighten that.
- **An honest qwen vs Haiku comparison.**

Judges probe numbers, so the methodology is worth the strongest model.

### 4. The investigation upgrade (when ready)
`SUPERAGENT_IMPLEMENTATION_PLAN.md`, currently on hold by choice. The most architecturally
complex item:
- structured hypotheses and decisions with revision checks
- non-blocking drone flights
- approvals tied to a specific version of the evidence

Weaker models tend to make a mess of this.

### 5. Real hardware, or a hardware emulator
Port the node to ESP32-S3 or nRF firmware, or run it under the Renode emulator: the int8 model on
a microcontroller, Ed25519 signing on the chip, and a real LoRa packet. Low-level and full of
gotchas, and the strongest proof of the "edge AI" claim.

### 6. A security pass on the evidence chain
- **Replay protection reopens after the tamper demo** (finding 3 in `CHATOVERFLOW_FINDINGS.md`).
- **Forged packets stored without limit** (finding 4).
- **An outside-in review** of the station API and the Photon bridge.

The tamper demo is in the pitch, so this matters.

## Better done with cheaper models
- multi-drone drawing on the map; phone inset placement; a recorded-demo static build for Vercel
- WhatsApp via Photon
- an export to EarthRanger/SMART (mostly glue code)
- docs, judge handouts, slides

## If there is only time for a few
**1 → 2 → 3:** fewer false alarms, no backlog, and believable numbers make the strongest demo.
**5** is the "wow" option if a hardware or emulator story matters to the judges.
