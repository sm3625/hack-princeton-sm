"""Deployment profiles: loading, validation, zero-drone agent, multi-drone sim, /api/deployment."""

import json
from typing import ClassVar

import pytest
from agent_fakes import Harness
from fastapi.testclient import TestClient

import agent.commander as commander_mod
from agent.llm import ROLES
from agent.mock import MockChatModel
from agent.tools import make_tools
from common.contracts import SoundClass as S
from common.deployment import (
    DEFAULT_DEPLOYMENT,
    DEPLOYMENT_DIR,
    NO_AERIAL,
    DeploymentError,
    load_deployment,
)
from sim.drone import BATTERY_PCT_PER_KM, RESERVE_PCT, SPEED_MPS
from sim.engine import SimEngine

SHIPPED = sorted(p.stem for p in DEPLOYMENT_DIR.glob("*.yaml"))


# ---------- loading and validation ----------


def test_three_profiles_ship():
    assert {"laguna_del_tigre_pilot", "no_drone", "well_resourced"} <= set(SHIPPED)
    for name in SHIPPED:
        assert load_deployment(name).name == name


def test_default_profile_reproduces_todays_drone(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT", raising=False)
    dep = load_deployment()
    assert dep.name == DEFAULT_DEPLOYMENT == "laguna_del_tigre_pilot"
    ((drone_id, spec),) = dep.drone_specs()
    assert drone_id == "drone-1"
    assert (spec.speed_mps, spec.reserve_pct) == (SPEED_MPS, RESERVE_PCT)
    assert 100.0 / spec.range_km == BATTERY_PCT_PER_KM
    assert spec.base_x_m is None  # starts at the map base
    assert dep.agent.check_interval_min == 30 and dep.agent.max_followup_checks is None


def test_env_var_picks_the_profile(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT", "well_resourced")
    dep = load_deployment()
    assert dep.drones.count == 3
    assert [i for i, _ in dep.drone_specs()] == ["scout-1", "scout-2", "heavy-1"]
    specs = dict(dep.drone_specs())
    assert specs["heavy-1"].speed_mps == 12 and specs["heavy-1"].range_km == 30
    assert specs["scout-1"].speed_mps == 15 and specs["scout-2"].base_x_m == 3200


def _write(tmp_path, body: str):
    (tmp_path / "bad.yaml").write_text(body)
    return tmp_path


OK = "site: {name: X}\n"


@pytest.mark.parametrize(
    "body",
    [
        "drones: {count: 1}\n",  # no site
        OK + "drones: {count: -1}\n",
        OK + "drones: {count: 99}\n",
        OK + "drones: {count: 1, spec: {speed_mps: 0}}\n",
        OK + "drones: {count: 1, spec: {reserve_pct: 150}}\n",
        OK + "drones: {count: 1, spec: {base_x_m: 100}}\n",  # x without y
        OK + "drones: {count: 1, units: [{id: a}, {id: b}]}\n",  # more units than drones
        OK + "drones: {count: 2, units: [{id: a}, {id: a}]}\n",  # duplicate ids
        OK + "ranger_teams: {count: 0}\n",
        OK + "ranger_teams: {response_min: 0}\n",
        OK + "connectivity: carrier-pigeon\n",
        OK + "agent: {check_interval_min: 0}\n",
        OK + "agent: {max_followup_checks: 0}\n",
        OK + "colour: green\n",  # unknown key
        "- not\n- a mapping\n",
        "site: [unclosed\n",
    ],
)
def test_bad_values_are_rejected(tmp_path, body):
    with pytest.raises(DeploymentError):
        load_deployment("bad", _write(tmp_path, body))


def test_the_approval_gate_cannot_be_switched_off(tmp_path):
    for body in (
        OK + "approval: {required: false}\n",
        OK + "approval: {enabled: false}\n",
        OK + "approval: false\n",
    ):
        with pytest.raises(DeploymentError):
            load_deployment("bad", _write(tmp_path, body))
    assert load_deployment("no_drone").public()["approval"]["required"] is True


def test_unknown_or_unsafe_profile_names(tmp_path):
    with pytest.raises(DeploymentError, match="Available"):
        load_deployment("nope")
    with pytest.raises(DeploymentError):
        load_deployment("../deployments/no_drone")


def test_minimal_profile_gets_safe_defaults(tmp_path):
    dep = load_deployment("bad", _write(tmp_path, OK))
    assert dep.drones.count == 1 and dep.ranger_teams.count == 1 and dep.connectivity == "lora"


# ---------- zero drones: agent shape, prompts, plan, mock flow ----------


class Recording(MockChatModel):
    """Mock that remembers which tools each role was given."""

    bound: ClassVar[dict] = {}

    def bind_tools(self, tools, **kw):
        names = [getattr(t, "name", None) for t in tools]
        Recording.bound[self.role] = [n for n in names if n]
        return super().bind_tools(tools, **kw)


def logging_incident(h: Harness) -> str:
    h.hear("Arroyo Recodo", 41, "02:14:05", S.CHAINSAW, 0.91)
    h.hear("Paso Musgo", 17, "02:14:09", S.CHAINSAW, 0.84)
    h.hear("Brecha Vieja", 23, "02:14:12", S.CHAINSAW, 0.86)
    h.tick("02:14:20")
    (inc,) = h.commander.incidents.all()
    return inc.id


def test_zero_drone_tools_have_no_drone_tools(tmp_path):
    h = Harness(tmp_path, deployment=load_deployment("no_drone"))
    tools = make_tools(h.commander.services)
    assert not {"plan_route", "fly_drone", "get_drone_status"} & set(tools)
    assert {"dispatch_rangers", "schedule_check", "verify_signature"} <= set(tools)


def test_zero_drone_agent_has_no_drone_subagent_or_prompt(tmp_path, monkeypatch):
    seen = {}
    real = commander_mod.create_deep_agent

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(commander_mod, "create_deep_agent", spy)
    Recording.bound = {}
    Harness(tmp_path, models={r: Recording(role=r) for r in ROLES}, deployment=load_deployment("no_drone"))
    assert [s["name"] for s in seen["subagents"]] == ["triage", "evidence"]
    prompt = seen["system_prompt"]
    assert "drone-pilot" not in prompt and "fly" not in prompt.lower()
    assert NO_AERIAL in prompt and "Remote CONAP post" in prompt and "90 min" in prompt
    assert "schedule_check(incident_id, 45)" in prompt and "at most 3 scheduled checks" in prompt
    for s in seen["subagents"]:
        assert "drone" not in s["system_prompt"].lower().replace("no drone", "")
    assert seen["interrupt_on"]["dispatch_rangers"]["allowed_decisions"] == ["approve", "edit", "reject"]


def test_default_profile_keeps_the_drone_subagent(tmp_path, monkeypatch):
    seen = {}
    real = commander_mod.create_deep_agent
    monkeypatch.setattr(commander_mod, "create_deep_agent", lambda **kw: seen.update(kw) or real(**kw))
    Harness(tmp_path, deployment=load_deployment("laguna_del_tigre_pilot"))
    assert [s["name"] for s in seen["subagents"]] == ["triage", "drone-pilot", "evidence"]
    assert "ask the drone-pilot sub-agent" in seen["system_prompt"]
    assert "schedule_check(incident_id, 30)" in seen["system_prompt"]


def test_zero_drone_mock_flow_asks_for_dispatch_with_the_aerial_note(tmp_path):
    h = Harness(tmp_path, deployment=load_deployment("no_drone"))
    inc_id = logging_incident(h)
    assert h.sim.flights == []
    inc = h.commander.incidents.get(inc_id)
    (h.commander.files.root / "incidents" / inc_id / "plan.md").unlink()
    h.commander._ensure_plan(inc)  # the seed checklist code writes before the agent runs
    seed = h.file(inc_id, "plan.md").lower()
    assert "drone" not in seed.replace("no aerial", "") and "ranger to verify" in seed
    assert "drone-pilot" not in {m["agent"] for m in h.of_type("agent_step")}
    (card,) = h.of_type("approval")
    assert NO_AERIAL in card["question"]
    # a person still has to approve, and the note is recorded with the dispatch
    assert h.commander.incidents.get(inc_id).dispatches == []
    h.commander.decide(card["approval_id"], "approve")
    inc = h.commander.incidents.get(inc_id)
    assert inc.status == "dispatched" and NO_AERIAL in inc.dispatches[0]["note"]
    assert inc.next_check_at == h.sim.t + 30 * 60  # mock schedules 30; the profile only guides real models


def test_dispatch_note_is_stamped_when_the_model_forgets_it(tmp_path):
    h = Harness(tmp_path, deployment=load_deployment("no_drone"))
    inc_id = logging_incident(h)
    tools = make_tools(h.commander.services)
    cfg = {"configurable": {"incident_id": inc_id}}
    h.commander.services.gate.grant(inc_id)
    out = tools["dispatch_rangers"].invoke({"sector": "E5", "note": "Chainsaws heard."}, cfg)
    assert out.startswith("Rangers dispatched") and NO_AERIAL in out


def test_followup_check_cap(tmp_path):
    h = Harness(tmp_path, deployment=load_deployment("no_drone"))  # cap 3
    inc_id = logging_incident(h)
    tools = make_tools(h.commander.services)
    cfg = {"configurable": {"incident_id": inc_id}}
    replies = [tools["schedule_check"].invoke({"incident_id": inc_id, "minutes": 10}, cfg) for _ in range(4)]
    assert all(r.startswith("Check scheduled") for r in replies[:3])
    assert replies[3].startswith("No more checks")


def test_every_profile_runs_the_mock_walkthrough(tmp_path_factory):
    for name in SHIPPED:
        h = Harness(tmp_path_factory.mktemp(name), deployment=load_deployment(name))
        inc_id = logging_incident(h)
        (card,) = h.of_type("approval")
        h.commander.decide(card["approval_id"], "approve")
        h.tick("03:22:00")
        assert h.commander.incidents.get(inc_id).status == "closed", name
        assert "[Arroyo Recodo #41]" in h.file(inc_id, "case_file.md")


# ---------- several drones in the sim ----------

NEAR_SCOUT_2 = (3100.0, 1600.0)  # next to scout-2's base at (3200, 1700)


def fleet_engine(name="well_resourced"):
    engine = SimEngine(publish=lambda m: None, deployment=load_deployment(name))
    engine.clock.set_time(0)
    engine.clock.set_speed(1)
    for d in engine.drones:
        d.reset(100)
    return engine


def test_nearest_idle_drone_is_picked():
    engine = fleet_engine()
    assert [d.id for d in engine.drones] == ["scout-1", "scout-2", "heavy-1"]
    route = engine.plan_route(*NEAR_SCOUT_2)
    assert route.feasible and route.drone_id == "scout-2"
    base_route = engine.plan_route(400.0, 400.0)  # next to the map base: scout-1 and heavy-1 start there
    assert base_route.drone_id == "scout-1"  # tie on distance goes to the first
    assert len(engine.drone_statuses()) == 3


def test_busy_or_low_battery_drones_are_skipped():
    engine = fleet_engine()
    engine.drones[1].reset(20)  # scout-2 is nearest but nearly flat
    route = engine.plan_route(*NEAR_SCOUT_2)
    assert route.feasible and route.drone_id != "scout-2"
    flight, why = engine.drones[0].launch(engine.plan_route(400.0, 400.0))
    assert flight is not None, why
    again = engine.plan_route(400.0, 400.0)
    assert again.feasible and again.drone_id == "heavy-1"  # scout-1 is flying now


def test_all_drones_unable_gives_a_reason():
    engine = fleet_engine()
    for d in engine.drones:
        d.reset(10)
    route = engine.plan_route(*NEAR_SCOUT_2)
    assert not route.feasible and "battery" in route.reason


def test_fly_uses_the_chosen_drone_and_messages_keep_old_fields():
    sent = []
    engine = SimEngine(publish=sent.append, deployment=load_deployment("well_resourced"))
    engine.clock.set_time(0)
    engine.clock.set_speed(1)
    route = engine.plan_route(*NEAR_SCOUT_2)
    engine.fly(route, wait=False)
    msg = [m for m in sent if m["type"] == "drone"][-1]
    assert msg["id"] == "scout-2" and msg["flying"] is True
    assert {"x_m", "y_m", "battery_pct", "flying", "phase", "target", "path"} <= set(msg)
    flying = [d.id for d in engine.drones if d.status().flying]
    assert flying == ["scout-2"]
    state = engine.state()
    assert state["drone"]["id"] == "scout-1" and len(state["drones"]) == 3


def test_drone_spec_changes_speed_and_range():
    engine = fleet_engine()
    scout, heavy = engine.drones[0], engine.drones[2]
    assert scout.speed_mps == 15 and heavy.speed_mps == 12
    a = scout.plan_route(1500.0, 1500.0)
    b = heavy.plan_route(1500.0, 1500.0)
    assert b.battery_needed_pct < a.battery_needed_pct  # 30 km range drains slower than 20 km


def test_zero_drone_sim():
    engine = fleet_engine("no_drone")
    assert engine.drone is None and engine.drones == []
    route = engine.plan_route(1000.0, 1000.0)
    assert not route.feasible and "no drone" in route.reason
    assert engine.drone_statuses() == []
    assert engine.state()["drone"] is None
    assert "no drone" in engine.fly(route).lower()


def test_drone_tools_list_all_drones_and_name_the_one_flown(tmp_path):
    h = Harness(tmp_path, deployment=load_deployment("well_resourced"))
    engine = fleet_engine()
    h.commander.services.sim = engine
    tools = make_tools(h.commander.services)
    status = tools["get_drone_status"].invoke({})
    assert status.count("\n") == 2 and "scout-2" in status and "heavy-1" in status
    plan = json.loads(tools["plan_route"].invoke({"sector": "G5"}, {}))
    assert plan["drone"] in {"scout-1", "scout-2", "heavy-1"} and plan["feasible"]
    engine.clock.set_speed(1000)  # fly() blocks until arrival on the sim clock
    flown = tools["fly_drone"].invoke({"sector": "G5"}, {})
    assert flown.startswith("Flew") and f"Drone: {plan['drone']}." in flown


# ---------- API ----------


def test_api_deployment_is_read_only_and_has_no_secrets(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("NODE_PIPELINE", "off")
    monkeypatch.setenv("DEPLOYMENT", "no_drone")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    from station.app import create_app

    client = TestClient(create_app())
    res = client.get("/api/deployment")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "no_drone"
    assert body["site"]["name"] == "Remote CONAP post"
    assert body["drones"] == {"count": 0, "units": []}
    assert body["aerial_verification"] is False
    assert body["approval"] == {"approver": "post chief", "required": True}
    assert body["ranger_teams"] == {"count": 1, "response_min": 90}
    assert body["connectivity"] == "satellite"
    assert "sk-secret-value" not in res.text
    assert client.post("/api/deployment", json={}).status_code == 405
    assert client.put("/api/deployment", json={}).status_code == 405


def test_api_deployment_default_lists_drone_specs(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("NODE_PIPELINE", "off")
    monkeypatch.delenv("DEPLOYMENT", raising=False)
    from station.app import create_app

    body = TestClient(create_app()).get("/api/deployment").json()
    assert body["name"] == "laguna_del_tigre_pilot" and body["drones"]["count"] == 1
    assert body["drones"]["units"][0]["id"] == "drone-1"


def test_bad_deployment_fails_at_startup(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("DEPLOYMENT", "does_not_exist")
    from station.app import create_app

    with pytest.raises(DeploymentError):
        create_app()
