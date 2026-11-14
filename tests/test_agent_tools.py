"""Agent tools: exact work in plain code, names not ids, verified records only."""

import json

import pytest
from agent_fakes import Harness

from agent.tools import make_tools
from common.contracts import SoundClass as S


@pytest.fixture
def env(tmp_path):
    h = Harness(tmp_path)
    h.hear("Arroyo Recodo", 41, "02:14:05", S.CHAINSAW, 0.91)
    h.hear("Paso Musgo", 17, "02:14:09", S.CHAINSAW, 0.84)
    h.hear("Paso Musgo", 18, "02:14:10", S.CHAINSAW, 0.99, verified=False)
    (inc,) = h.commander.incidents.all()
    tools = make_tools(h.commander.services)
    cfg = {"configurable": {"incident_id": inc.id}}

    def call(name, **args):
        return tools[name].invoke(args, cfg)

    return h, inc, call


def test_get_detections_returns_verified_records_by_name(env):
    _h, _inc, call = env
    out = call("get_detections", sector="E5", window_s=1800)
    assert "[Arroyo Recodo #41] 02:14:05 chainsaw 0.91" in out
    assert "[Paso Musgo #17]" in out
    assert "#18" not in out  # unverified
    assert "node_id" not in out
    assert call("get_detections", sector="Z9").startswith("Unknown sector")


def test_node_info_weather_and_memory(env):
    h, _inc, call = env
    call("remember", topic="nodes", note="Arroyo Recodo: false alarms\nin heavy rain")
    info = json.loads(call("get_node_info", node_name="arroyo recodo"))
    assert info["node"] == "Arroyo Recodo" and info["sector"] == "E5"
    assert info["verified_alerts_24h"] == 1
    assert info["memory"] == ["2026-11-14: Arroyo Recodo: false alarms in heavy rain"]
    assert "Unknown node" in call("get_node_info", node_name="Node 7")
    h.sim.rain = {"B3"}
    assert call("get_weather", sector="b3") == "B3: raining, wind 25 kph"
    assert call("get_weather", sector="E5") == "E5: dry, wind 6 kph"


def test_dispatch_refuses_without_a_recorded_approval(env):
    h, inc, call = env
    assert call("dispatch_rangers", sector="E5", note="go").startswith("Refused")
    h.commander.services.gate.grant(inc.id)
    assert call("dispatch_rangers", sector="E5", note="go").startswith("Rangers dispatched to E5")
    assert call("dispatch_rangers", sector="E5", note="again").startswith("Refused")  # grant used once
    assert len(h.commander.incidents.get(inc.id).dispatches) == 1


def test_schedule_check_and_status(env):
    h, inc, call = env
    assert "Unknown incident" in call("schedule_check", incident_id="other", minutes=10)
    assert "in 30 min" in call("schedule_check", incident_id=inc.id, minutes=30)
    assert h.commander.wakes.pending_check(inc.id) == h.sim.now() + 1800
    assert "in 240 min" in call("schedule_check", incident_id=inc.id, minutes=9999)
    assert "Cannot close" in call("set_status", status="closed", reason="done")
    assert "marked dismissed" in call("set_status", status="dismissed", reason="rain")
    assert h.commander.incidents.get(inc.id).summary == "rain"


def test_drone_tools(env):
    h, inc, call = env
    assert "battery 100%" in call("get_drone_status")
    route = json.loads(call("plan_route", sector=inc.sector))
    assert route["feasible"] and route["distance_km"] > 0
    assert json.loads(call("plan_route", sector="F2"))["feasible"] is False  # no-fly zone
    assert call("fly_drone", sector="F2").startswith("Not flown")
    out = call("fly_drone", sector=inc.sector)
    assert "Camera: Fresh clearing, one vehicle." in out
    assert "flight to" in h.file(inc.id, "drone_log.md")


def test_verify_signature_rechecks_raw_bytes(env):
    h, _inc, call = env
    assert "signature valid" in call("verify_signature", node="Arroyo Recodo", seq=41)
    h.tamper("Arroyo Recodo", 41)
    assert "INVALID" in call("verify_signature", node="Arroyo Recodo", seq=41)
    assert "INVALID" in call("verify_signature", node="Paso Musgo", seq=18)
    assert "no such record" in call("verify_signature", node="Arroyo Recodo", seq=7)


def test_incident_evidence_counts_independent_nodes(env):
    _h, _inc, call = env
    out = call("get_incident_evidence")
    assert "[Arroyo Recodo #41] 02:14:05 chainsaw 0.91" in out and "[Paso Musgo #17]" in out
    assert "#18" not in out  # unverified never becomes evidence
    assert "Independent nodes: 2" in out and "Corroborated: YES" in out
    assert "Valid node names (use only these):" in out
    assert "node_id" not in out


def test_get_detections_reaches_back_to_incident_start_while_triaging(env):
    """A slow model at 60x asks for 'the last minute' long after the records arrived."""
    h, inc, call = env
    h.sim.t += 3 * 3600
    out = call("get_detections", sector="E5", window_s=60)
    assert "[Arroyo Recodo #41]" in out
    h.commander.services.gate.grant(inc.id)
    call("dispatch_rangers", sector="E5", note="go")
    out = call("get_detections", sector="E5", window_s=60)  # follow-up: is it quiet now?
    assert out.startswith("0 verified records")


def test_incident_evidence_does_not_count_rain_nodes_as_corroboration(env):
    h, _inc, call = env
    h.sim.rain = {"E5", "D4"}  # Arroyo Recodo and Paso Musgo both in rain
    out = call("get_incident_evidence")
    assert "Nodes in rain now: 2 of 2." in out
    assert "Corroborated: NO." in out
