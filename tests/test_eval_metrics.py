"""Scoring maths for the evaluation (no station involved)."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.contracts import load_world
from eval import metrics
from eval.report import write_report
from sim.scenario import ScenarioEvent
from station import routes_eval

WORLD = load_world()
NODE = {n.name: n for n in WORLD.nodes}
START = 1_000_000


def event(at, node, sound="chainsaw", label="real", incident=None, source="chainsaw"):
    return ScenarioEvent(at=at, node=node, node_id=NODE[node].id, sound=sound, label=label,
                         incident=incident, source=source)  # fmt: skip


EVENTS = [
    event(100, "Creek Bend", incident="logging"),
    event(130, "Mossy Gap", incident="logging"),
    event(500, "South Gate", "gunshot", incident="poachers", source="gunshot"),
    event(900, "Low Marsh", label="false_alarm", source="rain"),
    event(1000, "Low Marsh", label="false_alarm", source="rain"),
    event(1100, "Low Marsh", "vehicle", label="benign", source="truck"),
]


def row(node, seq, at, sound="chainsaw", confidence=0.9, verified=True):
    return {
        "node": node, "node_id": NODE[node].id, "seq": seq, "timestamp": START + at, "lat": 0.0,
        "lon": 0.0, "sound": sound, "confidence": confidence, "model_version": 1,
        "clip_hash": "00" * 16, "signature": "", "verified": verified,
    }  # fmt: skip


ROWS = [
    row("Creek Bend", 1, 101),
    row("Mossy Gap", 1, 133, confidence=0.7),  # below the baseline threshold
    row("Low Marsh", 1, 905),
    row("Low Marsh", 2, 1003, confidence=0.82),
    row("Low Marsh", 3, 1101, "vehicle", 0.85),
    row("South Gate", 1, 5000, "gunshot", 0.95),  # no scenario event near this time
    row("Ridge East", 1, 120, confidence=0.99, verified=False),  # unverified never alerts
]


def view(iid, records, approvals=(), steps=None, wall=1.0, nodes=None):
    return {
        "incident": {"id": iid, "nodes": nodes or sorted({r[0] for r in records}), "status": "open"},
        "records": [{"node": n, "seq": s} for n, s in records],
        "approvals": [{"status": a} for a in approvals],
        "stats": {"runs": 1, "wall_s": wall, "steps": steps or {}},
    }


def test_match_event_picks_nearest_on_the_same_node_within_window():
    assert metrics.match_event(EVENTS, START, "Low Marsh", START + 995) is EVENTS[4]
    assert metrics.match_event(EVENTS, START, "Low Marsh", START + 5000) is None
    assert metrics.match_event(EVENTS, START, "Ridge East", START + 100) is None


def test_group_of_only_real_events():
    assert metrics.group_of(EVENTS[0]) == "logging"
    assert metrics.group_of(EVENTS[3]) is None
    assert metrics.group_of(None) is None
    assert metrics.real_groups(EVENTS) == ["logging", "poachers"]


def test_baseline_alerts_every_confident_verified_detection():
    b = metrics.score_baseline(ROWS, EVENTS, START, WORLD)
    # Creek Bend (real), Low Marsh x3 (false), South Gate gunshot with no event (false)
    assert b["alerts"] == 5
    assert b["real_caught"] == 1 and b["real_missed"] == 1
    assert b["false_alerts"] == 4
    assert b["caught"] == ["logging"]


def test_commander_counts_requests_catches_and_misses():
    views = [
        view("a", [("Creek Bend", 1), ("Mossy Gap", 1)], approvals=["approved", "stale"], steps={"triage": 2, "commander": 3}, wall=4.0),
        view("b", [("Low Marsh", 1)], approvals=["rejected"], steps={"triage": 1}, wall=2.0),
        view("c", [("Low Marsh", 2)], steps={"triage": 1}, wall=3.0),
    ]
    c = metrics.score_commander(views, ROWS, EVENTS, START)
    assert c["incidents"] == 3 and c["requests"] == 2
    assert c["real_caught"] == 1 and c["real_missed"] == 1 and c["caught"] == ["logging"]
    assert c["false_requests"] == 1 and c["false_dispatches"] == 0
    assert c["wall_s_per_incident"] == 3.0 and c["wall_s_per_real_incident"] == 4.0
    assert c["triage_steps_total"] == 4 and c["steps_total"] == 7


def test_a_rejected_real_incident_is_missed_and_an_approved_false_one_is_a_false_dispatch():
    views = [
        view("a", [("Creek Bend", 1)], approvals=["rejected"]),
        view("b", [("Low Marsh", 1)], approvals=["approved"]),
    ]
    c = metrics.score_commander(views, ROWS, EVENTS, START)
    assert c["real_caught"] == 0 and c["real_missed"] == 2
    assert c["false_dispatches"] == 1 and c["false_requests"] == 1


def test_noisy_node_averages_only_incidents_that_node_alone_heard():
    per = [
        {"nodes": ["Low Marsh"], "triage_steps": 2, "steps": 4, "wall_s": 3.0},
        {"nodes": ["Low Marsh"], "triage_steps": 0, "steps": 2, "wall_s": 1.0},
        {"nodes": ["Creek Bend", "Low Marsh"], "triage_steps": 9, "steps": 9, "wall_s": 9.0},
    ]
    n = metrics.noisy_node(per)
    assert n == {"node": "Low Marsh", "incidents": 2, "triage_steps": 1.0, "steps": 3.0, "wall_s": 2.0}
    assert metrics.noisy_node([])["incidents"] == 0


def test_eval_approves_real_incidents_and_rejects_the_rest():
    truth = {("Creek Bend", 1): "logging", ("Low Marsh", 1): None}
    assert metrics.want_dispatch(view("a", [("Creek Bend", 1)]), truth)
    assert not metrics.want_dispatch(view("b", [("Low Marsh", 1)]), truth)


def run_result(mode="mock", scenario="night1", memory="fresh", triage=2.0, pace="live", speed=60.0, baseline=None):
    return {
        "scenario": scenario, "mode": mode, "memory": memory, "model": mode, "partial": None,
        "pace": pace, "speed": speed,
        "real_incidents": ["logging", "poachers"],
        "baseline": baseline or {"alerts": 30, "real_caught": 2, "real_missed": 0, "false_alerts": 28},
        "commander": {
            "requests": 3, "real_caught": 2, "real_missed": 0, "false_requests": 1,
            "wall_s_per_incident": 1.5,
            "noisy_node": {"node": "Low Marsh", "incidents": 5, "triage_steps": triage, "steps": 4.0, "wall_s": 1.0},
            "triage_steps_total": int(triage * 10), "steps_total": 50, "wall_s_total": 12.0,
        },
    }  # fmt: skip


def test_report_has_the_table_and_the_memory_comparison(tmp_path):
    data = {"runs": {
        "night1:mock:fresh": run_result(),
        "night2:mock:fresh": run_result(scenario="night2", triage=2.0),
        "night2:mock:carried": run_result(scenario="night2", memory="carried", triage=0.5),
    }}  # fmt: skip
    out = tmp_path / "report.md"
    write_report(data, out)
    text = out.read_text()
    assert "| Rules baseline | 30 | 2/2 | 0 | 28 | none |" in text
    assert "| Commander, Mock model, live 60x | 3 | 2/2 | 0 | 1 | 1.5 s |" in text
    assert "5 triage steps with night 1's memory against 20 without it" in text


def test_report_uses_recorded_speed_and_includes_mock_waits(tmp_path):
    live = run_result(speed=12)
    waits = run_result(pace="waits", speed=1)
    data = {"runs": {"night1:mock:fresh": live, "night1:mock:fresh:waits": waits}}
    out = tmp_path / "report.md"
    write_report(data, out)
    text = out.read_text()
    assert "Commander, Mock model, live 12x" in text
    assert "Commander, Mock model, sim waits" in text
    assert "live 60x" not in text


def test_report_shows_each_baseline_when_variant_baselines_differ(tmp_path):
    baseline = {"alerts": 25, "real_caught": 1, "real_missed": 1, "false_alerts": 24}
    data = {"runs": {
        "night1:mock:fresh": run_result(),
        "night1:ollama:fresh": run_result("ollama", baseline=baseline),
    }}
    out = tmp_path / "report.md"
    write_report(data, out)
    text = out.read_text()
    assert "Rules baseline (Mock model, live 60x) | 30 | 2/2 | 0 | 28" in text
    assert "Rules baseline (Local model (ollama), live 60x) | 25 | 1/2 | 1 | 24" in text


def test_report_labels_legacy_run_without_speed_as_live(tmp_path):
    run = run_result("ollama")
    run.pop("speed")
    run.pop("pace")
    out = tmp_path / "report.md"
    write_report({"runs": {"night1:ollama:fresh": run}}, out)
    text = out.read_text()
    assert "Local model (ollama), live | 3 |" in text
    assert "live 60x" not in text


def test_report_flags_fewer_requests_with_worse_recall(tmp_path):
    run = run_result()
    run["commander"]["requests"] = 2
    run["commander"]["real_caught"] = 1
    out = tmp_path / "report.md"
    write_report({"runs": {"night1:mock:fresh": run}}, out)
    text = out.read_text()
    assert "Fewer requests came with worse recall, a regression against the baseline." in text


def test_report_calls_equal_or_higher_memory_triage_no_reduction(tmp_path):
    data = {"runs": {
        "night2:mock:fresh": run_result("mock", scenario="night2", triage=2.0),
        "night2:mock:carried": run_result("mock", scenario="night2", memory="carried", triage=2.0),
        "night2:ollama:fresh": run_result("ollama", scenario="night2", triage=2.0),
        "night2:ollama:carried": run_result("ollama", scenario="night2", memory="carried", triage=2.1),
    }}
    out = tmp_path / "report.md"
    write_report(data, out)
    text = out.read_text()
    assert "Night 1's memory did not reduce triage steps." in text
    assert "Night 1's memory increased triage steps." in text


def test_report_explains_sim_waits_pacing_limit(tmp_path):
    out = tmp_path / "report.md"
    write_report({"runs": {"night1:mock:fresh:waits": run_result(pace="waits")}}, out)
    text = out.read_text()
    assert "clock advances during drone flights" in text
    assert "not a strict lockstep replay with identical observations" in text


def test_eval_latest_route_serves_results_or_404(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(routes_eval.router)
    client = TestClient(app)
    monkeypatch.setenv("EVAL_RESULTS", str(tmp_path / "missing.json"))
    assert client.get("/api/eval/latest").status_code == 404
    path = tmp_path / "results.json"
    path.write_text("not json")
    monkeypatch.setenv("EVAL_RESULTS", str(path))
    assert client.get("/api/eval/latest").status_code == 404
    path.write_text(json.dumps({"runs": {"night1:mock:fresh": run_result()}}))
    res = client.get("/api/eval/latest")
    assert res.status_code == 200 and "night1:mock:fresh" in res.json()["runs"]
