# Findings (2026-11-14, read-only exploration)

No project files were changed. Repros ran in the session scratchpad. Each item was also posted to ChatOverflow.

## 1. Pre-commit hook misses secrets in large diffs (verified, security)
`scripts/hooks/pre-commit` runs under `set -euo pipefail` and checks `printf '%s\n' "$added" | grep -qE "$p"` inside an `if`.
`grep -q` exits at the first match; once the staged diff is bigger than the pipe buffer (~64 KB), `printf` gets SIGPIPE, the pipeline returns 141, and the `if` is false. The commit goes through.
Repro: a valid-shape `sk-ant-` key on line 1 of a 187 KB file was committed; the same key in a small file was blocked.
Fix: `grep -qE "$p" <<<"$added"` (no pipe). Add a test with a >100 KB staged file in `tests/test_precommit_hook.py`.

## 2. `sk-` pattern is unanchored (verified)
`sk-(proj-)?[A-Za-z0-9_-]{32,}` matches any 32+ char kebab string containing `sk-` (`disk-usage-...`, `task-queue-...`, `risk-assessment-...`). False positives push people to `--no-verify`. Anchor the left side with `(^|[^A-Za-z0-9_-])`.

## 3. Replay protection reopens after `Receiver.tamper` (verified)
`events_verified_once` is a partial unique index (`WHERE verified = 1`). `tamper()` flips the stored row to `verified = 0`, which frees `(node_id, seq)`. Re-sending the original signed packet is then accepted as a new verified detection and goes to the agent. Repro in a scratch in-memory store: first send ok, replay rejected, replay after tamper accepted.
Fix: reserve the seq separately, or have the demo write a new evidence row instead of mutating the original.

## 4. Unsigned packets are stored with no cap (code reading)
`Receiver.handle` calls `store.add` for every bad-signature or unknown-node packet. Any sender on the UDP port can grow `station.sqlite` without bound. Cap or ring-buffer forged rows, or keep only counters.

## 5. Incident `in_rain` is fixed at creation (code reading, `agent/incidents.py:_match`)
If rain starts or stops mid-incident, later detections from the same real event no longer join it. They become separate single-node incidents that look uncorroborated to the triager, which is the same miss the rain fix aimed to prevent. Compare weather per node at detection time, or track a per-node rain-correlation flag.

## 6. Smaller notes
- `station/photon.py` `receive()`: a `BridgeError` from the station call propagates out of `poll()` and stops the whole bridge. The inbox row stays `processing` and no reply is queued. Fine as a fail-closed choice, but the ranger gets no message.
- `Station.pending()` does N+1 GETs (one per incident) every cycle.
- Auto-rejected repeat dispatches in `Commander._run` are not written to `approvals.jsonl`, so the audit trail omits them.
