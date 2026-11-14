# Quick judgment eval (qwen3:8b)

4 of 4 correct in 153 s. Each case is one Night 1 incident on its own,
stopped at the decision. Not a full-night score: see report.md for that.

| Case | Expected | Decision | OK | Time | Model steps | Invented names |
|---|---|---|---|---|---|---|
| A: Logging: chainsaw at 3 dry nodes | dispatch_requested | dispatch_requested | yes | 41.1 s | 11 | none |
| C: Rain cluster: chainsaw at 2 nodes in rain | no_request | dismissed | yes | 28.0 s | 9 | none |
| B: Gunshots: 2 dry nodes | dispatch_requested | dispatch_requested | yes | 45.2 s | 14 | none |
| D: Single node in rain | no_request | dismissed | yes | 38.7 s | 8 | none |
