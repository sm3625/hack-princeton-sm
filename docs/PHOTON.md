# Photon ranger approvals

Implemented: pending Forest Guardian approvals can be sent to one allowlisted ranger's
Photon iMessage conversation. The ranger replies `APPROVE <code>` or
`REJECT <code> [reason]`. Replies use the existing `/api/approvals/{id}` endpoint and its
pending-interrupt, stale-request and one-use dispatch checks. This does not improve the
classifier or model judgment; it makes the human approval gate reachable through iMessage.
Dispatch in the current application is still simulated.

## Safe local preview

Start the station with `make dev`, create an incident, then in another terminal:

```sh
make photon
```

This reads the local station and prints proposed notifications. It makes **zero Photon
requests**, writes no bridge database, sends nothing and never decides an approval, even
if `.env` contains credentials. No worker starts automatically with the app.

Tests use fake Photon transports and the real commander via FastAPI TestClient:

```sh
uv run pytest -q tests/test_photon.py tests/test_precommit_hook.py
```

No live account was connected, line provisioned, message sent, or authenticated Photon
API request made during implementation. Live delivery and actual project permissions
remain unverified. Public documentation and the public OpenAPI schema were consulted.

## Why REST when the supplied URL is MCP?

[Photon's hosted MCP server](https://photon.codes/docs/beta/ai-tools/mcp) is at
`https://mcp.photon.codes/mcp`. It uses OAuth and exposes account-wide management tools.
The runtime bridge instead uses the same Photon 2.0 platform's project-scoped REST API,
with only message listing and sending. It does not register an MCP server in Codex,
perform OAuth login, create projects, provision numbers, or change account settings.
This keeps account management and credit-spending tools away from the incident LLM.
`Photon` is a small replaceable transport class, separate from the station adapter and
approval logic. No SDK dependency was added.

Contract source: [public OpenAPI schema](https://api.photon.codes/openapi.json), inspected
2026-11-14. The beta uses `Authorization: Bearer` project keys; do not substitute the
legacy Spectrum/stable SDK or account service keys. Sends use
`POST /v1/projects/{id}/messages`, required `idempotency-key`, and the
`senderId`, `spaceId`, `content` body. A successful response means accepted, not delivered.

## Setup for a later explicitly authorized live test

Do not run live commands until credit use has been approved. Existing Photon access or
free credits are not treated as permission. No price or free allowance is assumed.

1. In Photon, obtain an existing project-scoped key and iMessage resource, agent user,
   ranger human user, and direct space IDs. Project/resource setup may cost credits;
   this bridge intentionally cannot perform it. Follow the
   [official iMessage setup](https://photon.codes/docs/beta/get-started/imessage).
   Shared lines require the human to initiate the conversation first.
   The agent, ranger, space and resource IDs come from your first message, not a dashboard
   page. Put only `PHOTON_PROJECT_ID` and `PHOTON_API_KEY` in `.env`, text the line `ping` in a
   new iMessage chat, then run `uv run python -m station.photon --discover`. It makes one
   read-only GET (no sends) and prints the four IDs to copy into `.env`.
2. Fill the `PHOTON_*` values from `.env.example` in the **git-ignored `.env`**. Use a
   `pho_sk_` project key, not an account-wide `pho_ask_` key. The pre-commit guard checks
   both key formats. Never put a key on the command line or in a tracked file.
3. Keep the local station running. Only after explicit permission, an example bounded
   session is:

   ```sh
   uv run python -m station.photon --live --allow-sends 2 --seconds 120
   ```

   This permits at most **two send attempts total** across notifications and acknowledgments,
   not two credit units. Failures count toward the limit. The first pending approval consumes
   one attempt; its decision acknowledgement consumes another. Multiple pending approvals
   can exhaust the allowance before acknowledgments. Decisions may still be processed after
   the send budget reaches zero, so the dashboard is authoritative. No additional allowance
   is granted automatically; every invocation requires new explicit flags.

`--live` without `--allow-sends` enables authenticated polling and incoming decisions but
**no sends**. It is not an offline test. The default duration is 600 seconds, maximum 3600;
polling runs for the requested duration, at most 720 cycles (one hour at the 5-second minimum) and five pages per cycle. Minimum
poll interval is five seconds. Network calls have 15-second timeouts, so an in-progress
cycle can outlast the duration. Ctrl-C stops the worker. Run one worker per conversation.

The optional `PHOTON_STATION_URL` must be a loopback HTTP origin. The bridge is intended
for the trusted local station, not deployment of its unauthenticated local API to the internet.

## Safety, state, and recovery

- Inbound messages must match the configured project, iMessage platform, human sender,
  exact direct space and two participant IDs, plus receiving agent when provided. Outgoing
  echoes, group chats and other senders are ignored without replies. Commands are parsed
  deterministically; natural-language "yes" cannot approve anything.
- Approval codes expire 15 wall-clock minutes after first discovery. Expiration is separate
  from accelerated simulation time. Each decision rechecks the current pending approval;
  the commander performs the final atomic gate check. Dashboard decisions remain supported.
- `photon.sqlite` (git-ignored) stores mappings, polling checkpoints, inbound message IDs
  and outgoing message state, but no API key. Keep this database across restarts. It contains
  incident text and ranger responses; treat it as local private application data. A target
  change requires a separate database so old messages cannot be sent to a new conversation.
- Polling uses resource + space filters, received direction, ascending creation time, a
  fixed time window across pagination and a 30-second overlap. First startup begins at the
  current time, intentionally skipping historical commands. Empty pages with continuation
  tokens are followed. Hitting the page limit stops without advancing the checkpoint.
- Outbound work is claimed durably before sending and uses a stable idempotency key. A
  timeout or crash leaves `attempting` state and is **never automatically resent**. Inspect
  Photon and the dashboard to reconcile it; do not delete state or restart with a new DB
  just to force a retry. Restarting with more allowance may send previously queued work.
- Inbound work is claimed before calling the station. A failure/crash can leave `processing`
  state with an uncertain outcome. It is not replayed: reconcile in the dashboard. This
  favors avoiding repeated actions over guaranteed delivery; it is not exactly-once delivery.
- HTTP errors stop the worker, without retries or response-body/credential logging. Redirects
  are refused. HTTP 402 does not trigger provisioning, replenishment or fallback billing.
- No status/chat commands, images, delivery receipts, webhook endpoint, or historical replay
  are implemented. The existing dashboard supplies details and recovery. Approvals and
  rejections are the intentionally small first integration.
