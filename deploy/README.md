# Hosted station and Edge AI

Vercel is the entry-point redirect configured in `vercel/vercel.json`. The
dashboard, API, WebSocket, and persistent inference worker run on the station
host, not inside a Vercel function.

Starting `uvicorn station.app:app` directly does not run the setup performed by
`python -m emulators.demo`. For the hosted judge service:

1. Prepare the four held-out recordings on a machine with the source datasets:
   `.venv/bin/python -m emulators.run --prepare emulators/audio`.
2. Copy `emulators/audio/` (including `manifest.json`) into the deployed checkout.
   The service user must be able to read it and `models/classifier.{tflite,json}`.
3. Install `deploy/edge-worker.conf` as
   `/etc/systemd/system/forest-guardian-judges.service.d/edge-worker.conf`.
   Ensure its EnvironmentFile does not override these settings with
   `EDGE_BACKEND=off` or `NODE_PIPELINE=off`.
4. Run `systemctl daemon-reload` and restart `forest-guardian-judges`.
5. With the existing judge login, verify `/api/edge/status` reports
   `connected: true`, then use Edge AI to classify a recording. The status
   window counter should increase and predictions should appear.

This runs the real model in a local worker on the host. It is labeled as local
inference, not Raspberry Pi hardware. Keep the existing authentication and
same-origin protection enabled.
