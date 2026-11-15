"""The demo's pre-made closed incident holds up to the same checks as a live one."""

import pytest
from agent_fakes import Harness

from agent.incidents import fmt_time
from sim.scenario import ScenarioError, _parse, load_scenario
from station.demo_seed import SEED_SEQ, seed_closed_incident


def seeded(tmp_path, **kw):
    h = Harness(tmp_path, **kw)
    scenario = load_scenario("demo")
    inc = seed_closed_incident(h.commander, scenario.earlier, scenario.start, key_for=h.nodes.keys.__getitem__)
    return h, scenario, inc


def test_demo_scenario_opens_with_a_closed_incident(tmp_path):
    h, scenario, inc = seeded(tmp_path)
    assert inc.status == "closed" and inc.dispatches and inc.observations
    assert inc.public(h.world)["earlier"] is True  # the dashboard leaves it out of this run's totals
    assert inc.updated_at < scenario.start  # finished before the run begins
    assert [i.id for i in h.commander.incidents.all()] == [inc.id]
    assert set(inc.node_ids()) == set(scenario.earlier.node_ids)


def test_seeded_case_file_passes_signature_and_citation_checks(tmp_path):
    h, scenario, inc = seeded(tmp_path)
    view = h.commander.case_file(inc.id)
    assert view["integrity"]["ok"] and view["integrity"]["valid"] == len(inc.records) == 3
    assert view["citations"]["ok"] and view["citations"]["cited"] == 3
    assert "cedar" in view["html"]
    first = int(scenario.start - scenario.earlier.minutes_before * 60)
    assert f"{fmt_time(first)} [Hondonada #{SEED_SEQ}]" in view["html"]  # station clock, not UTC
    assert "case_file.md" in h.commander.incident_view(inc.id)["files"]


def test_seeding_never_wakes_the_agent_or_asks_a_ranger(tmp_path):
    h, _, inc = seeded(tmp_path)
    assert not h.commander.approvals
    shown = [m["incident"] for m in h.messages if m.get("type") == "incident"]
    assert [(i["status"], i["earlier"]) for i in shown] == [("closed", True)]  # never shown half-made
    assert not h.sim.flights
    assert all(seq >= SEED_SEQ for _, seq in inc.records)


def test_seeding_twice_adds_nothing(tmp_path):
    h, scenario, _ = seeded(tmp_path)
    again = seed_closed_incident(h.commander, scenario.earlier, scenario.start, key_for=h.nodes.keys.__getitem__)
    assert again is None and len(h.commander.incidents.all()) == 1


def test_earlier_block_is_validated():
    from sim import world as worldmap

    world = worldmap.load()
    with pytest.raises(ScenarioError):
        _parse("x", {"earlier": {"nodes": ["Nowhere"]}}, world)
    with pytest.raises(ScenarioError):
        _parse("x", {"earlier": {"nodes": ["Hondonada"], "sound": "background"}}, world)
    assert _parse("x", {}, world).earlier is None
