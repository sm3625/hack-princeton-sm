"""The 2-minute demo: scenario shape and scorecard logic (no station, no model)."""

from eval import demo
from sim.scenario import load_scenario


def test_demo_scenario_is_short_and_busy():
    s = load_scenario("demo")
    assert s.summary()["hours"] <= 0.75  # 36 sim-min at 20x is under 2 minutes
    assert len(s.events) >= 10
    first = min(e.at for e in s.events)
    assert first <= 60  # a sound within ~3 s of wall time at 20x


def test_expected_groups_come_from_labels():
    groups = demo.expected_groups()
    assert {k: g["label"] for k, g in groups.items()} == {
        "rain-west": "false_alarm", "logging": "real", "poaching": "real"}
    assert groups["logging"]["nodes"] == {"Arroyo Recodo", "Paso Musgo", "Brecha Vieja"}


def test_scorecard_marks_requests_and_dismissals():
    groups = demo.expected_groups()
    t = demo.Tracker(t0=0.0)
    t.update([
        {"id": "a", "status": "open", "nodes": ["El Bajo"], "title": "rain"},
        {"id": "b", "status": "open", "nodes": ["Arroyo Recodo", "Paso Musgo"], "title": "saw"},
    ])
    t.update([
        {"id": "a", "status": "dismissed", "nodes": ["El Bajo"], "title": "rain"},
        {"id": "b", "status": "awaiting_approval", "nodes": ["Arroyo Recodo", "Paso Musgo"], "title": "saw"},
    ])
    rows = {r["incident"]: r for r in t.score(groups)}
    assert rows["rain-west"]["verdict"] == "correct"
    assert rows["logging"]["verdict"] == "correct" and rows["logging"]["agent"] == "asked a ranger"
    assert rows["poaching"]["verdict"] == "not opened"
