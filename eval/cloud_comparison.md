# Gemini versus Haiku: local project comparison

One live pass per provider on four identical isolated Deep Agents incident fixtures. Sound classification is not part of this benchmark.

| Provider | Model | Completed | Median agent time | Approval requests | Unapproved dispatches | Agent failures |
|---|---|---:|---:|---:|---:|---:|
| haiku | claude-haiku-5-5 | 4/4 | 33.5 s | 0 | 0 | 0 |
| gemini | gemini-3.5-flash | 4/4 | 49.5 s | 3 | 0 | 0 |

| Case | Haiku decision / time | Gemini decision / time |
|---|---|---|
| Logging: chainsaw at 3 dry nodes | watching / 37.5 s | dispatch_requested / 65.3 s |
| Rain cluster: chainsaw at 2 nodes in rain | watching / 35.4 s | dispatch_requested / 80.3 s |
| Gunshots: 2 dry nodes | watching / 31.7 s | dispatch_requested / 32.8 s |
| Single node in rain | watching / 13.2 s | dismissed / 33.7 s |

Gemini median agent time was 1.48× Haiku in this run. Both used the production agent prompts, tools, and a 2,048-token output cap; Gemini thinking was left at its provider default. No provider-specific prompt tuning was applied.

Gemini is functional but is not behaviorally equivalent: it asked for approval in the rainy multi-node case where the existing quick-eval target says no request. Haiku kept watching even in the two positive cases. The fixture camera observation (fresh clearing, one vehicle) is ambiguous under the current policy, so these four expectation matches must not be presented as validated accuracy. Both preserved the human-approval boundary.

Limitations: one pass, four synthetic scenarios, fake clock and instant drone; provider batches were sequential, not randomized. These times include multi-step tool orchestration, not just one model response. No token-cost comparison or long-run reliability claim is supported.

Gemini ADC connectivity probe: 1.81 s for a tiny READY response. Prior Haiku-style tool history was also accepted in a separate Gemini probe. The real localhost dashboard was verified to show Gemini and a connected edge worker.

Reproduce with `.venv/bin/python -m eval.compare_cloud --modes haiku gemini --repeats 3`. This invokes paid APIs. See [Gemini setup](../docs/GEMINI.md).
