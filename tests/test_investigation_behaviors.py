"""Scripted end-to-end adaptation fixtures; not evidence of real-model reasoning."""
import pytest
from agent_fakes import Harness

from agent.commander import Conflict
from common.contracts import SoundClass as S


def hear_pair(h, rain=False):
    if rain:
        h.sim.rain = {n.sector for n in h.world.nodes}
    h.hear("Arroyo Recodo", 1, "02:14:05", S.CHAINSAW, .95)
    h.hear("Paso Musgo", 1, "02:14:09", S.CHAINSAW, .92)
    return h.commander.incidents.all()[0]


def test_real_activity_during_rain_overturns_initial_false_alarm(tmp_path):
    h = Harness(tmp_path)
    h.sim.rain = {n.sector for n in h.world.nodes}
    h.hear("Arroyo Recodo", 1, "02:14:05", S.CHAINSAW, .95)
    h.tick("02:14:20")
    inc, = h.commander.incidents.all()
    assert inc.status == "dismissed"
    h.hear("Paso Musgo", 1, "02:14:40", S.CHAINSAW, .92)
    h.tick("02:15:00")
    assert h.pending()
    assert inc.observations[-1]["availability"] == "observed"
    assert inc.assessments[-1]["next_action"] == "propose_dispatch"
    assert not inc.dispatches


def test_unavailable_drone_discloses_missing_corroboration(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.sim.battery = 5
    h.tick("02:14:30")
    assert h.pending() and not h.sim.flights
    assert "no aerial verification" in h.pending()[0].note.lower()
    assert "No aerial verification" in inc.assessments[-1]["reason"]


@pytest.mark.parametrize("seen", ["no activity seen", "Inconclusive: dense canopy", "Only wildlife; no people"])
def test_contradictory_camera_changes_proposal_to_recheck(tmp_path, seen):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.sim.seen = {inc.sector: seen}
    h.tick("02:14:30")
    assert not h.pending()
    assert inc.status == "watching" and inc.next_check_at
    assert inc.observations[-1]["content"] == seen
    assert "coverage" in inc.assessments[-1]["reason"]
    assert inc.assessments[-1]["next_action"] == "request_ranger_assessment"


def test_rain_and_unavailable_drone_stay_uncertain(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h, rain=True)
    h.sim.battery = 5
    h.tick("02:14:30")
    assert not h.pending()
    assert inc.status == "watching" and inc.next_check_at


def test_ranger_correction_rejects_old_approval_and_persists(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.tick("02:14:30")
    approval, = h.pending()
    h.commander.message(inc.id, "Permit checked; authorized crew", "authorized_activity")
    with pytest.raises(Conflict):
        h.commander.decide(approval.id, "approve")
    h.tick("02:15:00")
    assert not inc.dispatches and not h.pending()
    assert inc.status == "dismissed"
    assert "authorized activity" in inc.assessments[-1]["explanation"]
    restored = h.build().incidents.get(inc.id)
    assert restored.ranger_updates == inc.ranger_updates
    assert restored.assessments == inc.assessments


def test_freeform_correction_triggers_reassessment(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.tick("02:14:30")
    h.commander.message(inc.id, "Camera view missed the north trail; check coverage")
    h.tick("02:15:00")
    assert inc.status == "watching" and inc.next_check_at
    assert "north trail" in inc.assessments[-1]["reason"]
    assert not inc.dispatches


def test_confirmed_ranger_threat_overrides_rain_hypothesis(tmp_path):
    h = Harness(tmp_path)
    h.sim.rain = {n.sector for n in h.world.nodes}
    h.hear("Arroyo Recodo", 1, "02:14:05", S.CHAINSAW, .9)
    h.tick("02:14:30")
    inc, = h.commander.incidents.all()
    assert inc.status == "dismissed"
    h.commander.message(inc.id, "Ranger on site confirms active threat", "confirmed_threat")
    h.tick("02:15:00")
    assert h.pending() and not inc.dispatches


def test_freeform_message_does_not_erase_authorized_outcome(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.commander.message(inc.id, "Permit verified", "authorized_activity")
    h.commander.message(inc.id, "Crew remains on site")
    h.tick("02:15:00")
    assert inc.ranger_outcome() == "authorized_activity"
    assert not h.pending() and not inc.dispatches


def test_cancelled_flight_is_not_a_camera_observation(tmp_path):
    from agent.tools import make_tools
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.sim.fly = lambda route: "Not completed: the simulation was reset"
    result = make_tools(h.commander.services)["fly_drone"].invoke(
        {"sector": inc.sector}, {"configurable": {"incident_id": inc.id}})
    assert result.startswith("Not completed")
    assert inc.observations[-1]["availability"] == "unavailable"
    assert not h.file(inc.id, "drone_log.md")


def test_stale_assessment_cannot_overwrite_ranger_correction(tmp_path):
    from agent.tools import make_tools
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.commander.message(inc.id, "Permit verified", "authorized_activity")
    result = make_tools(h.commander.services)["record_assessment"].invoke(
        {"explanation": "Old threat claim", "alternative": "Noise", "next_action": "propose_dispatch", "reason": "Old evidence"},
        {"configurable": {"incident_id": inc.id, "ranger_revision": 0}})
    assert result.startswith("Refused:")
    assert len(inc.assessments) == 1 and inc.assessments[0]["source"] == "ranger"


def test_already_executed_dispatch_is_not_erased_by_correction(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.tick("02:14:30")
    h.commander.decide(h.pending()[0].id, "approve")
    assert len(inc.dispatches) == 1
    h.commander.message(inc.id, "Permit verified", "authorized_activity")
    h.tick("02:15:00")
    assert len(inc.dispatches) == 1
    assert inc.status == "watching"
    assert not h.pending()


def test_one_rainy_node_repeated_is_not_independent_confirmation(tmp_path):
    h = Harness(tmp_path)
    h.sim.rain = {n.sector for n in h.world.nodes}
    for seq, stamp in enumerate(["02:14:05", "02:14:35", "02:15:05"], 1):
        h.hear("Arroyo Recodo", seq, stamp, S.CHAINSAW, .99)
    h.tick("02:15:30")
    assert not h.pending() and not h.sim.flights


def test_racing_correction_invalidates_approval_before_click(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.tick("02:14:30")
    approval, = h.pending()
    # A graph started at revision 0 can finish after a correction was recorded.
    h.commander.incidents.update(inc.id, ranger_updates=[{
        "outcome": "authorized_activity", "text": "Permit verified", "source": "ranger",
        "timestamp": h.sim.now(), "records_at_update": len(inc.records)}])
    h.commander.wakes.message(inc.id, "Permit verified")
    with pytest.raises(Conflict, match="Ranger information changed"):
        h.commander.decide(approval.id, "approve")
    h.tick("02:15:00")
    assert not h.pending() and not inc.dispatches
    assert inc.status == "dismissed"


def test_new_confirmed_outcome_can_reopen_previous_ranger_clearance(tmp_path):
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.tick("02:14:30")
    h.commander.message(inc.id, "Permit checked", "authorized_activity")
    h.tick("02:15:00")
    assert not h.pending()
    h.commander.message(inc.id, "Separate armed group now confirmed on site", "confirmed_threat")
    h.tick("02:16:00")
    assert h.pending() and not inc.dispatches


def test_model_cannot_skip_camera_reassessment(tmp_path, monkeypatch):
    from agent import mock
    plain = mock.POLICIES["commander"]
    def skip_assessment(messages):
        start, brief = mock.last_human(messages)
        done = mock.Done(mock.calls_since(messages, start + 1))
        if done.has("task:drone-pilot"):
            return [mock.tc("dispatch_rangers", sector=brief.get("sector"), note="Dispatch without reassessment")]
        return plain(messages)
    monkeypatch.setitem(mock.POLICIES, "commander", skip_assessment)
    h = Harness(tmp_path)
    inc = hear_pair(h)
    h.sim.seen[inc.sector] = "Fresh clearing, one vehicle"
    h.tick("02:14:30")
    assert not h.pending() and not inc.dispatches
    assert inc.status == "watching" and inc.next_check_at
    assert "Assessment unavailable" in inc.summary


def test_inconclusive_camera_cannot_be_overridden_by_model_confidence(tmp_path):
    from agent.tools import make_tools
    h = Harness(tmp_path)
    inc = hear_pair(h)
    inc.observations = [{"availability": "observed", "content": "Inconclusive: dense canopy; no activity seen."}]
    make_tools(h.commander.services)["record_assessment"].invoke(
        {"explanation": "Acoustic confidence is high despite camera uncertainty", "alternative": "Weather",
         "next_action": "propose_dispatch", "reason": "Strong sensor confidence"},
        {"configurable": {"incident_id": inc.id}})
    assert "explicitly inconclusive" in h.commander._proposal_problem(inc)
    h.commander.message(inc.id, "Ranger on site confirms active threat", "confirmed_threat")
    # A fresh assessment is still required after the ranger update.
    assert "current record_assessment" in h.commander._proposal_problem(inc)
