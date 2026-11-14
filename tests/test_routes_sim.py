import importlib
import time

import pytest
from fastapi.testclient import TestClient

from sim import engine as engine_mod

QUICK = """
title: Quick
start: "22:00"
end: "22:05"
events:
  - {at: "22:00:03", node: Creek Bend, sound: chainsaw, confidence: 0.91}
  - {at: "22:00:04", node: Creek Bend, sound: background}
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "mock")
    import shutil

    from sim.scenario import SCENARIO_DIR

    for name in ("night1", "night2"):
        shutil.copy(SCENARIO_DIR / f"{name}.yaml", tmp_path / f"{name}.yaml")
    (tmp_path / "quick.yaml").write_text(QUICK)
    engine_mod.reset_engine(engine_mod.SimEngine(scenario_dir=tmp_path))
    import station.app

    importlib.reload(station.app)
    with TestClient(station.app.create_app()) as c:
        yield c
    engine_mod.reset_engine(None)


def test_world_lists_named_nodes_no_fly_and_base(client):
    world = client.get("/api/world").json()
    assert len(world["nodes"]) == 12
    assert {"Low Marsh", "Creek Bend", "Ridge North"} <= {n["name"] for n in world["nodes"]}
    assert world["no_fly"] == ["F2", "G6"]
    assert world["base"] == {"x_m": 200, "y_m": 200}
    assert world["cols"] == world["rows"] == 8


def test_lifespan_binds_the_engine(client):
    assert engine_mod.get_engine()._loop is not None


def test_incident_deep_link_serves_the_app(client):
    res = client.get("/incident/2026-11-14_E4")
    assert res.status_code == 200
    assert "Forest Guardian" in res.text


def test_start_pause_speed(client):
    res = client.post("/api/sim/scenario", json={"name": "night1", "speed": 60})
    assert res.status_code == 200
    state = res.json()
    assert state["scenario"] == "night1" and state["running"] and state["speed"] == 60
    assert state["feed"] == "stub"
    assert abs(state["sim_time"] - state["start"]) < 120
    assert {"name": "night1", "title": "Night 1", "hours": 8.0, "events": 40} in state["scenarios"]

    assert client.post("/api/sim/pause").json()["paused"] is True
    assert client.post("/api/sim/pause").json()["paused"] is False
    assert client.post("/api/sim/pause", json={"paused": True}).json()["paused"] is True
    assert client.post("/api/sim/speed", json={"speed": 10}).json()["speed"] == 10
    assert client.get("/api/sim/state").json()["paused"] is True


@pytest.mark.parametrize(
    "body,status",
    [
        ({"name": "nope"}, 404),
        ({"name": "world"}, 404),
        ({"name": "../night1"}, 404),
        ({"name": "night1", "speed": 100}, 422),
        ({"name": "night1", "speed": 0}, 422),
        ({"name": "night1", "from": "2pm"}, 422),
        ({"name": "night1", "from": "25:99"}, 422),
        ({"name": "night1", "from": "07:00"}, 422),
    ],
)
def test_bad_scenario_requests(client, body, status):
    assert client.post("/api/sim/scenario", json=body).status_code == status


def test_stub_detection_reaches_the_websocket(client):
    with client.websocket_connect("/ws") as ws:
        client.post("/api/sim/scenario", json={"name": "quick", "speed": 60})
        seen = []
        for _ in range(200):
            msg = ws.receive_json()
            seen.append(msg["type"])
            if msg["type"] == "detection":
                break
        assert "scenario" in seen and "clock" in seen
        det = msg["detection"]
        assert msg["stub"] is True
        assert det["node_id"] == 5 and det["sound"] == "chainsaw" and det["seq"] == 1
        assert det["confidence"] == 0.91 and det["verified"] is True


def test_start_from_a_clock_time(client):
    state = client.post("/api/sim/scenario", json={"name": "night1", "from": "02:10"}).json()
    assert 0 <= state["sim_time"] - (state["start"] + 4 * 3600 + 10 * 60) < 60


def test_scenario_stops_at_its_end(client, tmp_path):
    (tmp_path / "short.yaml").write_text('start: "22:00"\nend: "22:00:30"\nevents: []\n')
    client.post("/api/sim/scenario", json={"name": "short", "speed": 60})
    for _ in range(60):
        state = client.get("/api/sim/state").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["running"] is False and state["paused"] is True
    assert 0 <= state["sim_time"] - state["end"] <= 1
    assert "type" not in state
