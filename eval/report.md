# Evaluation

Rules baseline against the incident commander, replayed through the real pipeline.

| Night 1 | Alerts to rangers | Real caught | Real missed | False alerts | Time per incident |
|---|---|---|---|---|---|
| Rules baseline (Mock model, live 60x) | 29 | 2/2 | 0 | 20 | none |
| Commander, Mock model, live 60x | 11 | 2/2 | 0 | 8 | 2.9 s |
| Rules baseline (Local model (qwen3:8b), live 60x) | 29 | 2/2 | 0 | 20 | none |
| Commander, Local model (qwen3:8b), live 60x | 0 | 0/2 | 2 | 0 | 70.0 s |
| Rules baseline (Local model (qwen3:8b), sim waits) | 21 | 1/2 | 1 | 14 | none |
| Commander, Local model (qwen3:8b), sim waits (partial) | 15 | 1/2 | 1 | 10 | 781.7 s |

| Night 2 | Requests | Triage steps | All steps | Commander time | El Bajo triage each |
|---|---|---|---|---|---|
| Mock model, live 60x, no memory | 2 | 73 | 191 | 10.6 s | 16 (1) |
| Mock model, live 60x, after night 1 | 2 | 78 | 200 | 10.0 s | 16 (1) |

On night 1 the commander (mock model, live 60x) put 11 requests in front of rangers where the baseline sent 29 alerts (fewer), and caught 2 of 2 real incidents against the baseline's 2; 8 of its requests were false against 20 false baseline alerts. On night 1 the commander (local model (qwen3:8b), live 60x) put 0 requests in front of rangers where the baseline sent 29 alerts (fewer), and caught 0 of 2 real incidents against the baseline's 2; 0 of its requests were false against 20 false baseline alerts. Fewer requests came with worse recall, a regression against the baseline. On night 1 the commander (local model (qwen3:8b), sim waits) put 15 requests in front of rangers where the baseline sent 21 alerts (fewer), and caught 1 of 2 real incidents against the baseline's 1; 10 of its requests were false against 14 false baseline alerts. That run was cut short (stopped at the 45 min wall-time cap), so its numbers are partial. On night 2 the commander (mock model, live 60x) took 78 triage steps with night 1's memory against 73 without it. Night 1's memory increased triage steps.

Real incidents come from the scenario files. The baseline alerts on every verified detection at 0.8 or above. The commander's requests are approved when the incident is real and rejected otherwise, so a false alert is a request a ranger had to turn down. Time per incident is the commander's own working time, approval waits excluded; the baseline has no analysis step. Live runs use the recorded simulation speed. Sim waits holds the clock during agent work, but the clock advances during drone flights. This is pacing assistance, not a strict lockstep replay with identical observations. Older runs without speed metadata are labeled simply live.
