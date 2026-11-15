# Forest Guardian brag plan

**What it is:** Solar sensor nodes hear chainsaws, gunshots and trucks in a protected forest, sign every detection, and an AI incident commander checks each alert, flies a drone, and wakes a ranger only when it matters.
**For:** Park rangers covering huge areas with tiny teams, who learn to ignore alarm systems that cry wolf.
**Sets it apart:** Edge classifier (55 KB int8) + Ed25519-signed 108-byte records + agent that triages, sends a drone first, asks a human before any dispatch, and writes a case file citing signed records. Runs on a laptop, offline on a local 8B model.
**Best claim:** 6 sensor alerts → 2 incidents → 1 ranger request.
**Hook:** A chainsaw at 22:33 in Gunung Leuser National Park, heard as a real waveform (real ESC-50 chainsaw recording).
**Tone:** polished / cinematic. Dark, calm, Apple-like, matching the app's dark UI (#000 bg, #4cc27a green, #ff9f0a amber, #0a84ff blue, SF Pro).
**Length:** 92 s (user's demo cap is 2 min; brag's usual 20 s is stretched to tell the full flow to judges).
**Share caption:** Forest Guardian hears a chainsaw, checks it with a drone, and wakes a ranger only when it's real.

Privacy: sensor names are the app's illustrative names; no personal data appears. Sensors, drone and dispatch are simulated (said in the outro footnote).

## Music
120 BPM (beat 0.5 s, bar 2 s), A minor, synthesized in Python. Every cut lands on a bar line (even seconds).

## Storyboard
| # | Time | Scene | On screen |
|---|---|---|---|
| 1 | 0–6 | Hook | Night. "22:33 · Gunung Leuser National Park". Live waveform, real chainsaw audio, tag "chainsaw 0.99" |
| 1b | 6–18 | Stats (all later scenes shift +12 s; total 104 s) | Up to 30% of timber trade illegal (UNEP/INTERPOL 2012) · 12.3M snares in Cambodia/Laos/Vietnam protected areas (WWF 2020) · 72 km² per ranger (Appleton et al. 2022) |
| 2 | 6–14 | Problem | Alerts pile up: rain, insects, helicopter. "Most alarms are wrong. So rangers stop listening." |
| 3 | 14–20 | Reveal | Forest Guardian logo. "Wakes a ranger only when it matters." |
| 4 | 20–32 | On the sensor | Spectrogram → 55 KB model → 108-byte record assembles → Ed25519 seal. "Signed at the sensor." |
| 4x | +44–58 (final timeline) | Deep agent (later scenes shift +14 s more; total 118 s) | A: "What the commander weighs": 7 inputs with their real tools, converging on create_deep_agent → dispatch_rangers asks a ranger. B: "One commander. Three sub-agents.": triage / drone-pilot / evidence, Files, Memory, Skills, SQLite checkpointer, interrupt_on gate |
| 5 | 32–44 | Map | Real dashboard, push in, two nodes pulse, incident appears. "Two sensors agree. Dry night. Outside working hours." |
| 6 | 44–54 | Drone | Drone path draws to E5, camera report bubble. "It sends the drone before it wakes anyone." |
| 7 | 54–66 | Human gate | Approval card + iMessage: YES 3 → rangers dispatched. "Nothing moves without a ranger's yes." |
| 8 | 66–76 | Case file | Real case file scrolls, record citations highlight. "Every claim cites a signed record." |
| 9 | 76–84 | Result | 6 → 2 → 1 counts in. "Runs on a laptop. Offline on a local 8B model." |
| 10 | 84–92 | Outro | Logo, three parks, footnote. |
