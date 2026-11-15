"""Map sites: every profile's map loads with its scenarios, OSM files and site geography."""

import json
from pathlib import Path

import pytest

from common.contracts import load_world
from common.deployment import DeploymentError, load_deployment
from common.sites import (
    DEFAULT_MAP,
    available_maps,
    expand_sectors,
    in_hours,
    ranger_eta,
    site_map,
    site_notes,
)
from sim import world as worldmap
from sim.scenario import list_scenarios, load_scenario

ROOT = Path(__file__).resolve().parent.parent
NEW_MAPS = ["southern_cardamom", "gunung_leuser"]


def test_three_maps_and_profiles_name_them():
    assert available_maps() == [DEFAULT_MAP, "gunung_leuser", "southern_cardamom"]
    assert load_deployment("laguna_del_tigre_pilot").map == DEFAULT_MAP
    for key in NEW_MAPS:
        assert load_deployment(key).map == key


def test_unknown_map_is_rejected(tmp_path):
    (tmp_path / "x.yaml").write_text("site: {name: X}\nmap: atlantis\n")
    with pytest.raises(DeploymentError, match="no map 'atlantis'"):
        load_deployment("x", tmp_path)


@pytest.mark.parametrize("key", NEW_MAPS)
def test_site_world_scenarios_and_geo_agree(key):
    site = site_map(key)
    world = load_world(site.world_file)
    assert len(world.nodes) == 12 and len({n.name for n in world.nodes}) == 12
    for n in world.nodes:
        assert world.sector_of(n.x_m, n.y_m) == n.sector
        assert abs(world.origin_lat + n.y_m / 111320 - n.lat) < 2e-4
    assert world.ranger_posts and world.terrain and world.zones
    for t in world.terrain + world.zones:
        assert expand_sectors(t["sectors"])
    summaries = list_scenarios(site.scenario_dir, world)
    assert {s["name"] for s in summaries} >= {"night1", "demo"}
    for s in summaries:
        load_scenario(s["name"], site.scenario_dir, world)

    geo = ROOT / "ui" / "geo" / key
    block = json.loads((geo / "pilot_block.json").read_text())
    reserve = json.loads((geo / "reserve.json").read_text())
    assert block["origin"] == [world.origin_lat, world.origin_lon] and block["features"]
    assert reserve["reserve"]["rings"] and reserve["reserve_contains_pilot"] is not None
    assert sum(f.stat().st_size for f in geo.glob("*.json")) < 300_000

    shape = worldmap.to_json(world)
    assert shape["site"]["title"] and shape["ranger_posts"] and shape["zones"]
    assert "osm" not in shape["site"] and "research_queries" not in shape["site"]


def test_default_map_has_no_site_notes():
    world = load_world(site_map(DEFAULT_MAP).world_file)
    assert site_notes(world, "E4", 2250, 1750, {"chainsaw"}, 120, 45) == []
    assert ranger_eta(world, 2250, 1750, "E4", 45) is None


def test_zone_hours_wrap_midnight():
    assert in_hours("22:00-04:00", 23 * 60) and in_hours("22:00-04:00", 60)
    assert not in_hours("22:00-04:00", 12 * 60)
    assert in_hours(None, 0)


def test_leuser_plantation_noise_is_routine_by_day_and_flagged_at_night():
    world = load_world(site_map("gunung_leuser").world_file)
    zone = next(z for z in world.zones if "chainsaw" in z.get("expect", {}))
    sector = min(expand_sectors(zone["sectors"]))
    day = " ".join(site_notes(world, sector, 0, 0, {"chainsaw"}, 10 * 60, 40))
    night = " ".join(site_notes(world, sector, 0, 0, {"chainsaw"}, 2 * 60, 40))
    assert "consistent with lawful activity" in day
    assert "chainsaw OUTSIDE its normal hours" in night
    truck_at_night = " ".join(site_notes(world, sector, 0, 0, {"vehicle"}, 22 * 60, 40))
    assert "consistent with lawful activity" in truck_at_night


def test_cardamom_ridges_slow_rangers():
    world = load_world(site_map("southern_cardamom").world_file)
    steep = next(t for t in world.terrain if t.get("ranger_factor", 1) > 1)
    sector = max(expand_sectors(steep["sectors"]))
    x, y = worldmap.sector_center(world, sector)
    eta = ranger_eta(world, x, y, sector, 60)
    flat = ranger_eta(world.__class__(**{**world.__dict__, "terrain": []}), x, y, sector, 60)
    assert eta["eta_min"] > flat["eta_min"] and eta["terrain_factor"] > 1
    assert any("Terrain in" in line for line in site_notes(world, sector, x, y, set(), 0, 60))


def test_commander_evidence_and_dispatch_carry_site_notes(tmp_path):
    from agent_fakes import Harness

    from agent.tools import make_tools
    from common.contracts import SoundClass

    h = Harness(tmp_path, deployment=load_deployment("gunung_leuser"))
    h.hear("Pinggir Sawit", 1, "01:40:10", SoundClass.CHAINSAW, 0.91)
    h.hear("Sungai Landak", 1, "01:40:22", SoundClass.CHAINSAW, 0.86)
    (inc,) = h.commander.incidents.all()
    tools = make_tools(h.commander.services)
    cfg = {"configurable": {"incident_id": inc.id}}
    out = tools["get_incident_evidence"].invoke({}, cfg)
    assert "Site notes (map facts, not sensor evidence):" in out
    assert "Nearest ranger post:" in out
    eta = json.loads(tools["compare_response_options"].invoke({}, cfg))["ranger_eta"]
    assert eta["post"] and eta["eta_min"] > 10
    h.commander.services.gate.grant(inc.id)
    msg = tools["dispatch_rangers"].invoke({"sector": "E5", "note": "go"}, cfg)
    assert " from " in msg and "min)" in msg
    assert h.commander.incidents.get(inc.id).dispatches[0]["eta_min"] > 0


def test_site_picker_switches_the_whole_map():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sim.engine import SimEngine
    from station.routes_deployment import router

    class FakeReceiver:
        def set_world(self, world):
            self.world = world

    resets = []

    async def on_reset():
        resets.append(engine.deployment.map)

    app = FastAPI()
    app.include_router(router)
    app.state.deployment = profile = load_deployment("laguna_del_tigre_pilot")
    app.state.sim = engine = SimEngine(deployment=profile)
    app.state.receiver = receiver = FakeReceiver()
    client = TestClient(app)
    listing = client.get("/api/sites").json()
    assert listing["current"] == DEFAULT_MAP and not listing["editable"]
    assert [s["map"] for s in listing["sites"]] == available_maps()
    assert all(s["title"] and s["region"] for s in listing["sites"])
    assert client.put("/api/sites", json={"map": "gunung_leuser"}).status_code == 409

    engine.on_reset = on_reset
    assert client.put("/api/sites", json={"map": "atlantis"}).status_code == 404
    assert client.put("/api/sites", json={"map": "../x"}).status_code == 422
    body = client.put("/api/sites", json={"map": "gunung_leuser"}).json()
    assert body["current"] == "gunung_leuser" and resets == ["gunung_leuser"]
    assert engine.world.nodes[0].name == "Bukit Harimau" and receiver.world is engine.world
    assert engine.scenario_dir == site_map("gunung_leuser").scenario_dir
    assert app.state.deployment.name == "gunung_leuser" and engine.drone.speed_mps == 14
    names = {s["name"] for s in engine.state()["scenarios"]}
    assert names == {"night1", "demo"}
