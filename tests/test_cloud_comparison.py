from eval.compare_cloud import score, summarize


def test_agent_failures_and_unapproved_dispatch_cannot_pass():
    row = {'mode': 'gemini', 'status': 'completed', 'expected': 'no_request',
           'decision': 'watching', 'dispatches': 0, 'invented_names': [], 'wall_s': 2}
    assert score(row)
    assert not score(row | {'agent_failures': 1})
    assert not score(row | {'dispatches': 1})
    assert not score(row | {'invented_names': ['Imaginary sensor']})
    assert not score(row | {'status': 'timeout'})


def test_missing_results_are_not_zero_latency_measurements():
    assert summarize([], ['haiku', 'gemini'])['gemini']['median_decision_s'] is None
