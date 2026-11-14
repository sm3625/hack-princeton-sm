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

## Implementation status (2026-11-14)

The software work is integrated with the existing working-tree changes:

- Training mines confusing background categories on training folds, calibrates class
  thresholds on validation folds, and refuses exports over 100,000 bytes. Threshold
  comparisons use the exact rounded value written to metadata. Node baselines persist
  per device, are bounded, and never lower a calibrated threshold.
- Storm handling includes a conservative repeat-rain pre-filter, a priority executor,
  immediate corroborated heads-ups, and shorter wake-up evidence. Gunshots retain threat
  priority even in rain. A running model call is still non-preemptible.
- `python -m eval.run --lockstep --scenario night1 --mode mock` runs the real classifier,
  node signing, receiver and commander synchronously. Simulation time controls batching
  and retry timers. Drone flight delivers intervening events before returning at arrival.
  Inference takes zero simulated time; this measures decisions, not live queue throughput.
  The time cap is checked between agent calls, not a hard timeout on a hung provider.
- Scoring v2 counts executed dispatches with contemporaneous evidence snapshots.
  An approved request without execution earns no catch; later evidence cannot rescue
  an earlier false dispatch. Legacy results remain labeled as legacy in the report.
- Full and quick evals accept `--mode haiku`; quick results also save separate per-provider
  files. Cloud runs require credentials and are explicitly opt-in. No comparative speed,
  cost or accuracy claim is made without running both providers.
- Persistent replay reservations and bounded forged-packet retention are covered by the
  existing security tests. HTTP origin checking now rejects malformed origins cleanly;
  the WebSocket endpoint retains its own tested origin policy. Photon remains opt-in.

Validation: complete mock Night 1 (40 events, 29 detections, 2/2 real incidents caught)
repeated with identical detections, decisions, tool counts and scores. Complete mock
Night 2: 27 events, 22 detections, 1/1 caught. All scenario clips in these runs were
synthetic; these are pipeline/scheduler checks, not field accuracy measurements.

Still requires empirical work: retrain and evaluate a candidate on untouched test folds
before replacing the shipped model (its documented 6.9% background argmax false-positive
rate is unchanged), measure local versus cloud models, and test storm throughput at
normal speed. Hardware/LoRa firmware or Renode needs a separate target implementation;
no microcontroller execution is claimed. The investigation upgrade remains on hold as
specified above.
