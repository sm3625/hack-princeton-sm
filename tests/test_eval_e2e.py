"""Real replays through eval.run (opt-in: make test-e2e), about five minutes per pacing mode."""

import pytest

from eval.run import run_night

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("sim_waits", [False, True], ids=["live", "sim-waits"])
def test_replay_scores_the_logging_incident_for_both_the_baseline_and_the_commander(sim_waits):
    result, _ = run_night(
        "night1", "mock", from_time="02:10", max_wall_min=8,
        sim_waits=sim_waits, log=lambda *_: None,
    )
    assert result["partial"] is None
    assert result["baseline"]["alerts"] > 0
    assert "logging-e4" in result["baseline"]["caught"]
    c = result["commander"]
    assert "logging-e4" in c["caught"] and c["requests"] >= 1
    assert c["false_dispatches"] == 0  # the stand-in ranger rejects what is not real
    assert all(p["wall_s"] >= 0 for p in c["per_incident"])
