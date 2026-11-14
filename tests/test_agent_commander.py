"""Incident commander end to end, offline, on the scripted mock model (LLM_MODE=mock)."""

import json
from typing import ClassVar

import pytest
from agent_fakes import Harness
from langgraph.types import Command

from agent.commander import Conflict, NotFound
from agent.llm import ROLES
from agent.mock import MockChatModel
from common.contracts import SoundClass as S


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def logging_incident(h: Harness) -> str:
    """02:14: three nodes hear a chainsaw within seconds (the idea doc's walkthrough)."""
    h.hear("Creek Bend", 41, "02:14:05", S.CHAINSAW, 0.91)
    h.hear("Mossy Gap", 17, "02:14:09", S.CHAINSAW, 0.84)
    h.hear("Logging Road", 23, "02:14:12", S.CHAINSAW, 0.86)
    h.tick("02:14:20")
    (inc,) = h.commander.incidents.all()
    return inc.id


def test_walkthrough_02_14_to_close(h):
    inc_id = logging_incident(h)
    inc = h.commander.incidents.get(inc_id)
    # 02:14 incident folder + plan, triage verdict, drone flight, then it stops for a person
    assert inc.status == "awaiting_approval"
    assert "Triage" in h.file(inc_id, "plan.md")
    assert "real" in h.file(inc_id, "triage.md")
    assert "Fresh clearing, one vehicle" in h.file(inc_id, "drone_log.md")
    assert len(h.sim.flights) == 1
    agents = {m["agent"] for m in h.of_type("agent_step")}
    assert {"commander", "triage", "drone-pilot"} <= agents
    (card,) = h.of_type("approval")
    assert card["action"] == "dispatch_rangers" and card["sector"] == inc.sector
    assert "Dispatch a team" in card["question"] and "3 nodes agree" in card["question"]
    assert inc.dispatches == []

    # 02:22 ranger approves; agent schedules a 30 min check
    h.sim.t += 8 * 60
    h.commander.decide(card["approval_id"], "approve")
    inc = h.commander.incidents.get(inc_id)
    assert inc.status == "dispatched" and len(inc.dispatches) == 1
    assert inc.next_check_at == h.sim.t + 30 * 60
    assert json.loads(h.file(inc_id, "approvals.jsonl").splitlines()[0])["decision"] == "approved"

    # 02:52 the same node hears a vehicle: it joins the incident and is logged as a follow-up
    h.hear("Creek Bend", 42, "02:51:50", S.VEHICLE, 0.88)
    h.tick("02:52:00")
    assert len(h.commander.incidents.all()) == 1
    assert "Follow-up after dispatch" in h.file(inc_id, "plan.md")
    assert h.commander.incidents.get(inc_id).status == "dispatched"

    # later check: area quiet -> evidence sub-agent writes the case file, incident closes
    h.tick("03:22:00")
    inc = h.commander.incidents.get(inc_id)
    assert inc.status == "closed"
    case = h.file(inc_id, "case_file.md")
    for tag in ("[Creek Bend #41]", "[Mossy Gap #17]", "[Logging Road #23]", "[Creek Bend #42]"):
        assert tag in case
    assert "approved by ranger" in case
    report = h.commander.case_file(inc_id)
    assert report["citations"]["ok"] and report["citations"]["unknown"] == []
    assert report["integrity"]["ok"] and report["integrity"]["valid"] == 4
    assert "<script" not in report["html"]
    sectors = (h.commander.files.root / "memories" / "sectors.md").read_text()
    assert inc_id in sectors

    plans = h.of_type("plan")
    assert plans and all(p["incident_id"] == inc_id and "html" in p for p in plans)


def test_dispatch_blocks_until_approved(h):
    inc_id = logging_incident(h)
    inc = h.commander.incidents.get(inc_id)
    assert inc.status == "awaiting_approval" and inc.dispatches == []
    (a,) = h.pending()
    assert a.interrupt_id in h.commander.pending_interrupt_ids(inc_id)

    # more sound while waiting: logged, but the agent is not re-run past the open interrupt
    h.hear("Creek Bend", 43, "02:15:00", S.CHAINSAW, 0.9)
    h.tick("02:16:00")
    h.tick("02:40:00")
    assert h.pending() == [a]
    assert a.interrupt_id in h.commander.pending_interrupt_ids(inc_id)
    assert h.commander.incidents.get(inc_id).dispatches == []

    h.commander.decide(a.id, "approve")
    assert len(h.commander.incidents.get(inc_id).dispatches) == 1
    # the deferred detection wake runs after the approval resolved
    h.tick("02:41:00")
    assert h.commander.incidents.get(inc_id).status == "dispatched"


def test_reject_cancels_dispatch(h):
    inc_id = logging_incident(h)
    (a,) = h.pending()
    h.commander.decide(a.id, "reject", "Team is on another call")
    inc = h.commander.incidents.get(inc_id)
    assert inc.dispatches == []
    assert inc.status == "watching"
    assert inc.next_check_at is not None
    assert h.commander.pending_interrupt_ids(inc_id) == set()
    row = json.loads(h.file(inc_id, "approvals.jsonl"))
    assert row["decision"] == "rejected" and row["note"] == "Team is on another call"


def test_edit_changes_the_note_not_the_target(h):
    inc_id = logging_incident(h)
    (a,) = h.pending()
    h.commander.decide(a.id, "edit", "Approach from the river road, two vehicles seen")
    inc = h.commander.incidents.get(inc_id)
    (sent,) = inc.dispatches
    assert sent["note"] == "Approach from the river road, two vehicles seen"
    assert sent["sector"] == a.sector
    assert inc.status == "dispatched"


def test_approvals_only_act_on_a_pending_interrupt(h):
    inc_id = logging_incident(h)
    (a,) = h.pending()
    with pytest.raises(NotFound):
        h.commander.decide("not-a-real-id", "approve")
    with pytest.raises(ValueError):
        h.commander.decide(a.id, "edit", "")
    with pytest.raises(ValueError):
        h.commander.decide(a.id, "dispatch_now")

    # someone resolves the interrupt behind the registry's back: the card is now stale
    cfg = {"configurable": {"thread_id": f"incident-{inc_id}", "incident_id": inc_id}}
    h.commander.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": "x"}]}), cfg)
    with pytest.raises(Conflict):
        h.commander.decide(a.id, "approve")
    assert h.commander.approvals[a.id].status == "stale"
    assert h.commander.incidents.get(inc_id).dispatches == []


def test_decided_approval_cannot_be_reused(h):
    logging_incident(h)
    (a,) = h.pending()
    h.commander.decide(a.id, "approve")
    with pytest.raises(Conflict):
        h.commander.decide(a.id, "approve")
    assert len(h.commander.incidents.all()[0].dispatches) == 1


def test_unverified_records_never_reach_the_agent(h):
    h.hear("Creek Bend", 41, "02:14:05", S.CHAINSAW, 0.91)
    h.hear("Mossy Gap", 99, "02:14:07", S.CHAINSAW, 0.97, verified=False)  # bad signature
    h.commander.on_verified(h.nodes.record("Kapok Stand", 5, 0, S.CHAINSAW, 0.9, verified=False)[0])
    (inc,) = h.commander.incidents.all()
    assert [n for n, _ in inc.records] == [h.commander.services.node_by_name("Creek Bend").id]
    h.tick("02:14:20")
    seen = json.dumps(h.messages)
    assert "Mossy Gap #99" not in seen and "Kapok Stand #5" not in seen
    assert "Mossy Gap #99" not in (h.file(inc.id, "detections.jsonl") or "")
    assert "Mossy Gap #99" not in (h.file(inc.id, "triage.md") or "")


def test_tampered_record_is_excluded_from_the_case_file(h):
    inc_id = logging_incident(h)
    h.commander.decide(h.pending()[0].id, "approve")
    h.tamper("Mossy Gap", 17)
    h.tick("03:30:00")
    case = h.file(inc_id, "case_file.md")
    assert "[Mossy Gap #17]" not in case
    assert "Failed verification" in case and "Mossy Gap #17" in case
    report = h.commander.case_file(inc_id)
    assert report["integrity"]["invalid"] == ["Mossy Gap #17"] and not report["integrity"]["ok"]
    assert report["citations"]["ok"]


def test_case_file_citations_must_exist(h):
    inc_id = logging_incident(h)
    h.commander.files.write(
        f"/incidents/{inc_id}/case_file.md",
        "Saw it [Creek Bend #41]. Also [Creek Bend #999] and [Nowhere #1].",
    )
    report = h.commander.case_file(inc_id)
    assert not report["citations"]["ok"]
    assert report["citations"]["unknown"] == ["Creek Bend #999", "Nowhere #1"]


def test_rain_false_alarms_on_one_noisy_node_are_dismissed(h):
    h.sim.rain = {"B3"}
    times = ["03:00:00", "03:06:00", "03:12:00", "03:18:00", "03:24:00", "03:30:00"]
    for i, t in enumerate(times):
        h.hear("Low Marsh", 5 + i, t, S.CHAINSAW, 0.82)
        h.tick(t[:-2] + "15")
    (inc,) = h.commander.incidents.all()
    assert inc.status == "dismissed" and len(inc.records) == 6
    assert h.of_type("approval") == [] and inc.dispatches == [] and h.sim.flights == []
    nodes_memory = (h.commander.files.root / "memories" / "nodes.md").read_text()
    assert nodes_memory.count("Low Marsh") == 1  # learned once, then reused
    assert "false-alarm history" in h.file(inc.id, "triage.md")


def test_truck_heard_by_four_nodes_is_one_incident_and_not_dispatched(h):
    for node, seq, t in [("Logging Road", 1, "04:10:00"), ("Kapok Stand", 1, "04:10:08"),
                         ("Mossy Gap", 1, "04:10:12"), ("Creek Bend", 1, "04:10:20")]:
        h.hear(node, seq, t, S.VEHICLE, 0.9)
    h.tick("04:10:30")
    (inc,) = h.commander.incidents.all()
    assert inc.family == "vehicle" and len(inc.node_ids()) == 4
    assert inc.status == "watching" and h.of_type("approval") == []
    h.tick("04:31:00")  # scheduled check: no logging sounds followed
    assert h.commander.incidents.get(inc.id).status == "dismissed"


def test_each_wake_up_resumes_the_incident_thread_after_restart(h):
    inc_id = logging_incident(h)
    (a,) = h.pending()
    h.commander.stop()
    fresh = h.build()  # new process: same agent_fs/ and SQLite checkpoints
    assert fresh.incidents.get(inc_id).status == "awaiting_approval"
    fresh.decide(a.id, "approve")
    assert len(fresh.incidents.get(inc_id).dispatches) == 1
    state = fresh.agent.get_state({"configurable": {"thread_id": f"incident-{inc_id}"}})
    wakes = [m for m in state.values["messages"] if m.type == "human"]
    assert len(wakes) == 1 and "Wake-up: new detections" in wakes[0].content


def test_ranger_message_wakes_the_commander(h):
    inc_id = logging_incident(h)
    h.commander.decide(h.pending()[0].id, "approve")
    h.commander.message(inc_id, "Team on site, two people fled north.")
    h.tick("02:30:00")
    assert any("ranger's message" in m["summary"] for m in h.of_type("agent_step"))
    with pytest.raises(NotFound):
        h.commander.message("no-such-incident", "hi")


def test_bus_messages_use_names_not_ids(h):
    logging_incident(h)
    for m in h.messages:
        text = json.dumps(m)
        assert "node_id" not in text and "thread_id" not in text and "incident-20" not in text
    inc_msgs = h.of_type("incident")
    assert inc_msgs and set(inc_msgs[-1]["incident"]) >= {"id", "sector", "status", "title", "updated_at"}
    assert "Creek Bend" in inc_msgs[-1]["incident"]["nodes"]


class Recording(MockChatModel):
    bound: ClassVar[dict[str, list]] = {}

    def bind_tools(self, tools, **kw):
        Recording.bound[self.role] = list(tools)
        return super().bind_tools(tools, **kw)


def test_agent_shape(tmp_path):
    """Verified deepagents 0.7.38 behaviour we rely on (see the spike notes in the report)."""
    h = Harness(tmp_path, models={r: Recording(role=r) for r in ROLES})
    logging_incident(h)
    h.commander.decide(h.pending()[0].id, "approve")
    h.tick("03:30:00")
    names = {role: sorted(t.name for t in tools) for role, tools in Recording.bound.items()}
    assert names["commander"] == sorted(
        ["dispatch_rangers", "edit_file", "get_detections", "get_incident_evidence", "get_node_info", "get_weather", "ls",
         "read_file", "remember", "schedule_check", "set_status", "task", "write_file"]
    )
    # sub-agent tools replace the parent's: no dispatch, no task (no nested delegation)
    assert names["triage"] == ["get_detections", "get_incident_evidence", "get_node_info", "get_weather", "read_file", "write_file"]
    assert names["drone-pilot"] == ["fly_drone", "get_drone_status", "plan_route", "read_file"]
    assert names["evidence"] == ["get_detections", "get_incident_evidence", "ls", "read_file", "remember", "verify_signature", "write_file"]
    task = next(t for t in Recording.bound["commander"] if t.name == "task")
    listed = [ln.split(":")[0] for ln in task.description.splitlines() if ln.startswith("- ")]
    assert listed == ["- triage", "- drone-pilot", "- evidence"]  # no general-purpose sub-agent
    assert all("write_todos" not in v for v in names.values())
