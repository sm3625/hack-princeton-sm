# Idea: AI Ventilation Coach (Arduino air-quality sensor)

## Pitch
A $10 Arduino air-quality sensor + AI that tells a household or classroom how to make its air safer, without buying anything. Household air pollution from cooking with solid fuels kills ~3M people/year (WHO), mostly women and children; poorly ventilated classrooms spread airborne disease.

## Core insight: one sensor → ventilation rate
After a pollution spike (cooking, candle, spray), the reading decays exponentially once the source stops:

```
C(t) = C₀ · e^(−kt)
```

The decay rate `k` is the room's **air changes per hour (ACH)**, a real building-science metric normally measured with special equipment. Only the *shape* of the curve matters, so an uncalibrated sensor (e.g. MQ-135) works.

## What the AI does
- **Decay fitter (plain physics):** detects spikes, fits decay, outputs ACH. Exact and cheap.
- **Event classifier:** labels events by curve shape: sharp spike + fast fall = spray; slow build + long tail = cooking; plateau = poor ventilation.
- **Time-series foundation model** (Chronos / TimesFM / Moirai), zero-shot:
  - forecast ("unhealthy in 12 min at this rate")
  - anomalies (3am spike → leak or smoldering source)
- **Vision LLM:** phone photo of the room → room size, windows, fuel/stove type, chimney/hood. Grounds the advice in context.
- **LLM agent with tools:** runs experiments and builds an evidence log of what works in *that* home.
- **Voice interface** (Whisper + TTS) in the user's own language, for people who can't use a dashboard.

### Agent tools
```
get_sensor_window(minutes)    → recent readings
fit_decay(start, end)         → ACH + fit quality
forecast(horizon)             → time-series model prediction
analyze_room_photo(img)       → room context from vision model
set_fan(on/off)               → smart plug (optional)
log_experiment(condition, ACH)
```

## Architecture
```
Arduino sensor ──serial/WiFi──► Python backend
                                   ├─ decay fitter (physics: ACH)
                                   ├─ Chronos/TimesFM (forecast, anomalies)
Phone photo ──► vision LLM ───────►│
Voice ──► Whisper ────────────────►└─ LLM agent (tool calls) ──► advice, voice reply,
                                                                  fan control, weekly report
```

## Sample interaction
> **User (voice):** "I'm about to cook dinner, what should I do?"
> **Agent:** "Last three dinners with the window closed, smoke stayed above unhealthy levels for 45 minutes. With the window and door both open it cleared in 9. Open both before you light the stove, and keep the baby in the other room for the first 15 minutes."

## Demo plan
1. Light a match/candle near the sensor; the live graph spikes.
2. Blow it out; the model fits the decay and reports ACH.
3. Open a window or turn on a fan, repeat; show the faster decay and the coach's advice.

## Simpler alternatives
- **Calibration:** train a correction model against official AQI + weather data (AirNow, OpenAQ) so the cheap sensor tracks the reference monitor.
- **Hyperlocal spikes:** flag when your spot is much worse than the regional AQI, pointing to a local source.

## Notes
- MQ-135 is a broad sensor (VOCs, smoke, alcohol), not true ppm. Fine for decay shape.
- Must-have for the hackathon: sensor → decay fitter → LLM agent with tools. Vision and voice are stretch goals.
