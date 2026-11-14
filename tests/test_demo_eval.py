"""The 2-minute demo: scenario shape and scorecard logic (no station, no model)."""

from eval import demo
from sim.scenario import load_scenario


def test_demo_scenario_is_short_and_busy():
    s = load_scenario("demo")
    assert s.summary()["hours"] <= 0.7  # 38 sim-min at 20x is about 2 minutes
    assert len(s.events) >= 10
    first = min(e.at for e in s.events)
    assert first <= 60  # a sound within ~3 s of wall time at 20x


def test_expected_groups_come_from_labels():
    groups = demo.expected_groups()
    assert {k: g["label"] for k, g in groups.items()} == {
        "insects": "false_alarm", "logging": "real", "aircraft": "benign", "poaching": "real"}
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
    assert rows["insects"]["verdict"] == "correct"
    assert rows["logging"]["verdict"] == "correct" and rows["logging"]["agent"] == "asked a ranger"
    assert rows["poaching"]["verdict"] == "not opened"
    assert rows["aircraft"]["expected"] == "no ranger request"


def test_demo_uses_real_recordings():
    for e in load_scenario("demo").events:
        assert e.clip and e.clip.startswith(("ESC-50-master/", "UrbanSound8K/"))
