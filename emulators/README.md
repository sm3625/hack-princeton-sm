# Raspberry Pi deployment lab

Runs the production `TFLiteClassifier` unchanged, with one inference thread and its real
56,648-byte int8 model. Graphify's `Classifier` contract and the current implementation
were used to locate the isolated inference boundary. No station, API key, microphone,
network connection, or fallback heuristic is involved in inference.

## Connected sound-detection dashboard

```sh
.venv/bin/python -m emulators.demo
```

Open `http://127.0.0.1:8000`. The **Edge sound detection** panel opens when the worker
connects. Select a sensor and recording, then click **Detect sound**. Gunshot is a good
first demonstration: the prepared recording produced a verified detection in the browser
check. The normal scenario Play button also uses this same edge worker for classification.
The panel displays the backend, platform, model size, window count, predictions and latency.

The local command runs the exact production classifier in a persistent separate Python
process. For the ARM64 Linux container, after installing/starting Docker Desktop:

```sh
.venv/bin/python -m emulators.demo --backend arm64 --profile pi-zero2
# Or --profile pi4; use --port 8001 if the normal station occupies port 8000.
```

The launcher prepares missing demo recordings, builds the ARM image when selected, and
starts the existing station. A missing/failed ARM worker stops startup; it never silently
falls back. For an offline commander rehearsal use `--mock-agent`: only the commander is
mocked, while sound classification remains real. Otherwise your existing LLM configuration
is preserved. Use `LORA_LOSS=0` before the command for deterministic radio delivery in a video.
Restart an existing station with this launcher to enable the connection.

One shared worker serially serves all simulated sensor nodes. Only feature extraction and
classification execute in that worker; debounce, signing, clip storage, simulated radio,
verification and the commander stay in the station process. The container communicates over
stdin/stdout with networking disabled and receives audio windows, not signing keys. This
is not a full board or full node-runtime emulator.

Normal thresholds and the 30-second per-class suppression remain active. Replaying the same
sound immediately at the same sensor can produce zero new detections; choose another sensor
or let simulation time advance. Background should normally produce no incident. “Emitted”
means the node signed a record; the Feed shows whether radio delivery and verification
succeeded. The existing node audio controls can play the corresponding saved recording.

Validation: local browser testing confirmed nine real gunshot windows → one signed record →
a verified gunshot in the Feed and map update. ARM64 execution remains unverified on this
machine because Docker is absent. `EDGE_BACKEND=off` (the default for the normal station)
retains its original classifier behavior; `EDGE_BACKEND=local|arm64` and `EDGE_PROFILE`
can also configure the bridge directly for an existing station launch.

## Run now on this Mac (18 seconds)

From the repository root:

```sh
.venv/bin/python -m emulators.run --prepare emulators/audio
.venv/bin/python -m emulators.run --audio emulators/audio --live
open emulators/evidence/local/report.html
```

Preparation uses existing datasets and selects the first filename from held-out ESC-50
fold 5 (birds, chainsaw, engine) and UrbanSound8K fold 10 (gunshot), without consulting
predictions. It resamples to 16 kHz, repeats short clips to five seconds, and writes source
and prepared-file hashes in `audio/manifest.json`. This small sample demonstrates execution,
not accuracy. Respect the original datasets' licenses when sharing audio. To use your own
clips, pass a directory of mono 16-bit PCM 16 kHz WAVs with at least one second each.
Without `--audio`, the runner uses explicitly labeled synthetic smoke-test audio.

The local backend is a rehearsal: it does **not** emulate a Pi or impose resource limits.

## ARM64 Linux execution and resource envelopes

Install and start Docker Desktop before the live demo. On an Intel host Docker Desktop
uses QEMU for ARM64; on an ARM Mac it uses ARM64 Linux virtualization. These are Linux
userspace compatibility experiments, not complete Raspberry Pi board emulators.
The launcher refuses to substitute local execution when Docker is unavailable.

```sh
.venv/bin/python -m emulators.run --backend arm64 --profile pi-zero2 \
  --audio emulators/audio --output emulators/evidence/pi-zero2 --live
.venv/bin/python -m emulators.run --backend arm64 --profile pi4 \
  --audio emulators/audio --output emulators/evidence/pi4 --live
open emulators/evidence/pi-zero2/report.html
```

| Profile | Enforced Docker budget | Intended experiment |
| --- | --- | --- |
| pi-zero2 | 1 CPU quota, 384 MiB RAM, no swap | Conservative application envelope for a 512 MiB device |
| pi4 | 4 CPU quota, 1 GiB RAM, no swap | Larger ARM64 Linux application envelope |

These names describe deployment targets, not emulated SoCs or equivalent clock speeds.
The first profile deliberately leaves nominal space for the OS and uses less than the
board's full core count. The runner records actual cgroup limits, architecture, runtime
versions, model/source/input hashes, model load time, warm latency percentiles, deadline
misses, and process peak RSS. RSS excludes the OS and other processes. Timings include
feature extraction plus classification; they exclude audio decoding and warm-up. Container
memory covers more than process RSS. Quotas cannot predict speed on a physical Pi.

Build once before recording (requires internet); subsequent builds use the Docker cache.
Inference runs with networking disabled, read-only code/model, no added capabilities,
no swap, and a writable evidence directory. The build uses an explicit file allowlist;
it does not send repository secrets, keys, databases, or datasets to Docker. The runtime
checks Linux/aarch64 before labeling a report ARM64. Use separate output directories to
retain both profile reports. On plain x86 Linux, ARM64 execution requires configured
binfmt/QEMU; do not attempt a live setup during judging.

The image pins Python's minor version and the two direct inference dependencies. Its base
image and transitive dependencies are not digest/lock pinned; retain the built image for
repeat demos. The prepared audio directory must exist before mounting it.

## Two-minute video / live-demo script

- **0:00–0:20:** “This is the exact quantized model used by our sensor node. It's 55.3 KiB,
  and this wrapper imports the production classifier unchanged.” Show the model hash.
- **0:20–0:45:** Run the prebuilt ARM64 command with `--live`. Show ARM64 Linux and the
  resource budget. If using this Mac without Docker, explicitly say “local rehearsal”;
  do not claim ARM execution.
- **0:45–1:10:** Point out real predictions from the four recordings as they arrive.
  Explain that a one-second audio window advances every half second. Mixed predictions
  are real outputs; file names describe inputs, not asserted ground truth per window.
- **1:10–1:35:** Open that run's `report.html`. Show model size, measured P95, peak RSS,
  and deadline misses; expand evidence for hashes and enforced cgroup limits.
- **1:35–2:00:** “Successful ARM64 execution supports software portability to 64-bit Pi
  Linux. We still need physical hardware to measure actual Pi latency, microphone I/O,
  power, and thermal behavior.”

Keep a previously generated HTML/JSON report available offline as a recording backup.
It is a timestamped past result, not a simulated live run. Do not describe the host's
latency as Raspberry Pi performance. A genuine hardware benchmark remains future work.

## Validation status

Local inference and report generation are tested on this Mac. Docker and QEMU were absent
when this harness was implemented, so ARM64 image build/execution remain **unverified**
until those commands are run. A prepared Dockerfile alone is not ARM compatibility proof.

References: [Docker multi-platform execution](https://docs.docker.com/build/building/multi-platform/),
[LiteRT ARM64 distributions](https://pypi.org/project/ai-edge-litert/2.3.0/).
