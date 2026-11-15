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

## Keeping the public site up

Vercel only redirects to the station host, so failures can come from the station or its
TLS certificate (a 6-day Let's Encrypt IP cert). Safeguards:

- `systemd/restart.conf`: the station always restarts after a crash or OOM kill.
- `watchdog.sh` + `forest-guardian-watchdog.timer`: every 30 s, restarts the
  station after 3 failed `/api/health` checks, and checks hourly to renew the cert when it has
  under 2 days left.
- `nginx/station-down.conf`: during a restart visitors get a page that reloads
  itself instead of "502 Bad Gateway".
- `.github/workflows/uptime.yml`: checks Vercel, health and cert every 10 min;
  GitHub emails on failure.

Install on the host as root: `bash /opt/forest-guardian-judges/deploy/install-watchdog.sh`.
