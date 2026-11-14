# Scenarios

`world.yaml` is the shared map (4 × 4 km, sectors A1–H8, 12 named nodes, no-fly F2 and G6, base in A1).
Every other `*.yaml` here is a replayable night for the simulator (`sim/scenario.py`).

## Nights

| File | Sim time | Detections | Real incidents | Noise |
|---|---|---|---|---|
| `night1.yaml` | 22:00–06:00, 14 Nov | 40 | 2: chainsaw logging in E4 (9 records), gunshots near G2 then a vehicle (5 records) | 19 weather false alarms (18 rain + 1 thunder; 16 on Low Marsh), 1 supply truck heard by 4 nodes in 28 s, 1 branch fall read as a gunshot, 1 aircraft, 1 howler-monkey "chainsaw" |
| `night2.yaml` | 22:00–06:00, 15 Nov | 27 | 1: chainsaw logging in C7 after the rain stops (7 records) | Same pattern: 16 weather false alarms (14 on Low Marsh), a patrol truck heard by 3 nodes, a branch fall inside no-fly G6 |

Night 2 exists for the memory test: the commander should recognise the Low Marsh rain pattern from
night 1 and spend fewer triage steps on it, while still taking Fern Hollow seriously once the rain is over.

Drone sightings (anything else: "no activity seen"):

- night 1, E4: "fresh clearing, one vehicle" 02:10–02:55, then "fresh clearing, no one in sight".
- night 1, G2: "pickup truck heading west, two people" 04:37–04:50, then "fresh tyre tracks, no one in sight".
- night 2, C7: "two people with a chainsaw, motorbike nearby" 01:50–02:35, then "fresh stumps, no one in sight".

A sighting matches when the drone's target is within 300 m of the sector. From base, the drone reaches
E4, G2 (detouring around F2) and C7 on a full battery; Old Cedar (G8) and the far north-east are out of
range with the 15 % reserve, and G6 is no-fly.

## Format

```yaml
title: Night 1
date: 2026-11-14          # day the night starts (station local time)
start: "22:00"            # quote clock times: YAML 1.1 reads 02:14 as a number
end: "06:00"              # times after midnight belong to the next day
speed: 30                 # default sim speed, 1-60x

weather:                  # sectors: all | [B3, C4] | "A1:C6" (rectangle)
  - {at: "23:50", sectors: "A1:C6", rain: true, wind_kph: 24}

drone:                    # what the camera sees over a sector, optionally in a time window
  - {sector: E4, see: "fresh clearing, one vehicle", from: "02:10", until: "02:55"}

events:                   # one expected detection each
  - at: "02:14:05"        # or "+90" / 90 for seconds after start
    node: Creek Bend      # node name from world.yaml
    sound: chainsaw       # class the node should report: chainsaw | gunshot | vehicle | axe | background
    confidence: 0.91      # used only by the stub feed
    duration: 8           # seconds of audio fed to the node (max 30)
    clip: esc50/chainsaw_1.wav   # optional, relative to data/
    source: chainsaw      # what really made the sound (ground truth for eval)
    label: real           # real | false_alarm | benign
    incident: logging-e4  # groups the records of one real incident
```

Events on the same node and class are at least 30 s apart, matching the node's debounce.

## Audio

Each event's audio is the named clip from `data/` (git-ignored datasets) when it exists, resampled to
16 kHz mono and looped or trimmed to `duration`. Otherwise the runner synthesises a placeholder that
sounds like the class (two-stroke buzz, a short volley of shots, a diesel rumble, chops) and logs
`synthetic audio: ...`. Placeholders keep the pipeline runnable before the datasets are downloaded; they
say nothing about classifier accuracy. The clip names above are suggestions for whoever fetches the data.

## Running

- UI: pick a night, a speed, press play. Restart replays from the start.
- API: `POST /api/sim/scenario {"name": "night1", "speed": 30}`; add `"from": "02:10"` to skip ahead
  (earlier events are skipped, earlier weather still applies). `POST /api/sim/pause`, `POST /api/sim/speed`.
- Until the pipeline's NodeRuntime is plugged in (`get_engine().set_feed(...)`), detections come from the
  stub in `sim/demo_feed.py`, marked "stub" on the wire and in the UI.
