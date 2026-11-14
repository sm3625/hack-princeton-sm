# Evaluation

Rules baseline against the incident commander, replayed through the real pipeline.

| Night 1 | Alerts to rangers | Real caught | Real missed | False alerts | Time per incident |
|---|---|---|---|---|---|
| Rules baseline | 29 | 2/2 | 0 | 20 | none |
| Commander, Mock model | 11 | 2/2 | 0 | 8 | 2.9 s |

| Night 2 | Requests | Triage steps | All steps | Commander time | Low Marsh triage each |
|---|---|---|---|---|---|
| Mock model, no memory | 2 | 73 | 191 | 10.6 s | 16 (1) |
| Mock model, after night 1 | 2 | 78 | 200 | 10.0 s | 16 (1) |

On night 1 the mock model commander put 11 requests in front of rangers where the baseline sent 29 alerts (fewer), and caught 2 of 2 real incidents against the baseline's 2; 8 of its requests were false against 20 false baseline alerts. On night 2 the mock model commander took 78 triage steps with night 1's memory against 73 without it.

Real incidents come from the scenario files. The baseline alerts on every verified detection at 0.8 or above. The commander's requests are approved when the incident is real and rejected otherwise, so a false alert is a request a ranger had to turn down. Time per incident is the commander's own working time, approval waits excluded; the baseline has no analysis step.
