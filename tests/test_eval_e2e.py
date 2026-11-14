"""A real replay through eval.run (opt-in: make test-e2e). Mock model, night 1 from 02:10, ~1 min."""

import pytest

from eval.run import run_night

pytestmark = pytest.mark.e2e


def test_replay_scores_the_logging_incident_for_both_the_baseline_and_the_commander():
    result, _ = run_night("night1", "mock", from_time="02:10", max_wall_min=6, log=lambda *_: None)
    assert result["partial"] is None
    assert result["baseline"]["alerts"] > 0
    assert "logging-e4" in result["baseline"]["caught"]
    c = result["commander"]
    assert "logging-e4" in c["caught"] and c["requests"] >= 1
    assert c["false_dispatches"] == 0  # the stand-in ranger rejects what is not real
    assert all(p["wall_s"] >= 0 for p in c["per_incident"])
