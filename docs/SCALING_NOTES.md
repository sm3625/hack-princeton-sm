# Running 24/7: model speed, backlog and scaling

Notes from 2026-11-14. Estimates come from measured qwen3:8b runs on an Apple M5 laptop; nothing
here comes from a 24-hour test.

## Measured
- Quick eval (`make eval-quick`, 4 Night 1 incidents, each alone): 28–45 s per incident, 8–14
  model calls each, 4/4 correct.
- Full Night 1 replay at 60x with clock waits: the latest run hit its 45 min cap partway through
  the night, at 782 s of agent time per incident. That run predates the newest fixes, including
  the repeat-dispatch block.
- Why each wake-up is slow: every call re-reads a ~5–8k token deepagents prompt (system prompt +
  tool schemas). Switching between commander and sub-agent prompts defeats Ollama's prefix cache.
  Reasoning mode is already off.

## Why the eval backlog is not the live situation
- The eval compresses an 8-hour night into ~8 minutes of arrivals (60x). No laptop 8B model keeps
  up with that.
- The repeat-dispatch loop (about 10 re-requests for one rain incident) multiplied the work; code
  now refuses repeats until new evidence arrives.

## Normal-speed load estimate
- Night 1: ~40 detections over 8 h, grouped into ~15–20 incidents, so ~2–3 incidents per hour.
- Each incident: ~3–5 wake-ups (first decision, new records, scheduled checks) × 30–45 s ≈ 2–4 min of model time.
- Load ≈ 2.5/h × 3 min ≈ 7–10 min of work per hour → qwen is busy about 15% of the time; queue
  empties between incidents.

## When it can still pile up
- Storms: rain sets off many nodes at once (10+ incidents in minutes); the last waits several minutes.
- Larger deployments: 100 nodes instead of 12 is about 8x the load, which exceeds what qwen on one
  laptop can handle.
- Worst case: a real chainsaw queued behind rain false alarms during a storm.

## Fixes (not built yet), cheapest first
1. Code-side pre-filter: dismiss obvious cases without the model (e.g. a single node in rain
   with a false-alarm history). Removes most storm load.
2. Priority queue: dry multi-node chainsaw or any gunshot ahead of rain alarms.
3. Immediate heads-up: a high-confidence corroborated threat sends "possible chainsaw, reviewing"
   at once; the agent confirms, dismisses or escalates. The queue never delays the first alert.
4. Cloud overflow: if the queue is long and there is internet, route some incidents to Haiku.
5. Cheaper wake-ups: shorter prompts, fewer tools per sub-agent.

## qwen vs Haiku (estimates, not measured)
| | qwen3:8b on the Mac (measured) | Haiku 5.5 (estimate) |
|---|---|---|
| One incident alone | 28–45 s | ~10–20 s |
| Full night, 20+ incidents | 45+ min, did not finish | ~5–10 min |
| Cost | free | under 1¢ per incident ($0.10 / $0.50 per MTok); ~10–30¢ per night |

- Haiku removes local prompt processing and generation time, but calls stay sequential and add
  network round trips. Expect 2–4x per incident; far more for a full night because queueing stops
  compounding.
- Haiku needs internet: the "cloud when connected" tier, with qwen as the offline fallback.
- To measure: `make eval-quick` currently runs on Ollama only and needs a small change to accept
  `LLM_MODE=haiku` (costs a few cents; needs an OK and an API key in `.env`).

## Line for judges
At normal speed a laptop running a local 8B model is idle about 85% of the time. Code clears obvious
rain alarms before the AI sees them, real threats jump the queue, and the first alert never waits on the model.
