"""Grouping detections into incidents, location estimates, and wake-up timing (plain code)."""

import json

import pytest
from agent_fakes import Nodes, at

from agent.incidents import AgentFiles, IncidentManager, IncidentRepo, WakeQueue, centroid
from common.contracts import SoundClass as S
from common.contracts import load_world


@pytest.fixture
def world():
    return load_world()


@pytest.fixture
def mgr(world, tmp_path):
    files = AgentFiles(tmp_path / "fs")
    files.ensure()
    return IncidentManager(world, files, IncidentRepo(tmp_path / "i.sqlite"))


def add(mgr, nodes, name, seq, t, sound, conf=0.9, verified=True):
    return mgr.add(nodes.record(name, seq, at(t), sound, conf, verified)[0])


def test_truck_heard_by_four_nodes_is_one_incident(world, mgr):
    nodes = Nodes(world)
    hits = [("Logging Road", "04:10:00"), ("Kapok Stand", "04:10:08"), ("Mossy Gap", "04:10:12"), ("Creek Bend", "04:10:20")]
    incs = {add(mgr, nodes, n, 1, t, S.VEHICLE).id for n, t in hits}
    assert len(incs) == 1
    (inc,) = mgr.all()
    assert inc.family == "vehicle" and len(inc.records) == 4
    assert sorted(inc.public(world)["nodes"]) == ["Creek Bend", "Kapok Stand", "Logging Road", "Mossy Gap"]


def test_location_is_confidence_weighted_centroid(world, mgr):
    nodes = Nodes(world)
    add(mgr, nodes, "Creek Bend", 1, "02:14:05", S.CHAINSAW, 0.9)
    inc = add(mgr, nodes, "Mossy Gap", 1, "02:14:09", S.CHAINSAW, 0.3)
    cb, mg = world.node(5), world.node(8)
    assert inc.x_m == pytest.approx((cb.x_m * 0.9 + mg.x_m * 0.3) / 1.2)
    assert inc.y_m == pytest.approx((cb.y_m * 0.9 + mg.y_m * 0.3) / 1.2)
    assert inc.sector == world.sector_of(inc.x_m, inc.y_m)
    assert (inc.x_m, inc.y_m) == pytest.approx(centroid(world, inc.weights))
    assert inc.id == "2026-11-14_E5"  # folder named after where it was first heard


def test_far_apart_or_different_threats_are_separate(world, mgr):
    nodes = Nodes(world)
    a = add(mgr, nodes, "Ridge North", 1, "02:00:00", S.CHAINSAW)
    b = add(mgr, nodes, "River Mouth", 1, "02:00:05", S.CHAINSAW)  # ~4.3 km away
    c = add(mgr, nodes, "Ridge North", 2, "02:00:10", S.GUNSHOT)  # same place, different threat
    assert len({a.id, b.id, c.id}) == 3


def test_vehicle_joins_a_nearby_logging_incident(world, mgr):
    nodes = Nodes(world)
    a = add(mgr, nodes, "Creek Bend", 1, "02:14:05", S.CHAINSAW)
    b = add(mgr, nodes, "Creek Bend", 2, "02:51:50", S.VEHICLE)
    assert a.id == b.id and b.family == "logging" and b.sounds == ["chainsaw", "vehicle"]


def test_chainsaw_upgrades_a_vehicle_incident(world, mgr):
    nodes = Nodes(world)
    add(mgr, nodes, "Creek Bend", 1, "02:00:00", S.VEHICLE)
    inc = add(mgr, nodes, "Creek Bend", 2, "02:05:00", S.CHAINSAW)
    assert inc.family == "logging" and inc.title == "Chainsaw near Creek Bend"


def test_time_window_and_closed_incidents_start_new_ones(world, mgr):
    nodes = Nodes(world)
    a = add(mgr, nodes, "Creek Bend", 1, "01:00:00", S.CHAINSAW)
    b = add(mgr, nodes, "Creek Bend", 2, "03:00:00", S.CHAINSAW)  # two hours later
    assert b.id == "2026-11-14_E5-2" and a.id != b.id
    mgr.update(b.id, status="closed")
    c = add(mgr, nodes, "Creek Bend", 3, "03:05:00", S.CHAINSAW)
    assert c.id == "2026-11-14_E5-3"


def test_unverified_background_and_replayed_records_are_dropped(world, mgr):
    nodes = Nodes(world)
    assert add(mgr, nodes, "Creek Bend", 1, "02:00:00", S.CHAINSAW, verified=False) is None
    assert add(mgr, nodes, "Creek Bend", 2, "02:00:00", S.BACKGROUND) is None
    assert add(mgr, nodes, "Creek Bend", 3, "02:00:00", S.CHAINSAW) is not None
    assert add(mgr, nodes, "Creek Bend", 3, "02:00:00", S.CHAINSAW) is None
    (inc,) = mgr.all()
    assert inc.records == [[5, 3]]


def test_detections_jsonl_uses_names_and_record_tags(world, mgr):
    nodes = Nodes(world)
    inc = add(mgr, nodes, "Creek Bend", 41, "02:14:05", S.CHAINSAW, 0.91)
    (row,) = [json.loads(x) for x in mgr.files.read(inc.folder + "detections.jsonl").splitlines()]
    assert row["record"] == "[Creek Bend #41]" and row["node"] == "Creek Bend" and row["seq"] == 41
    assert "node_id" not in row and "signature" not in row
    assert row["time"] == "2026-11-14T02:14:05Z" and row["confidence"] == 0.91


def test_incidents_persist(world, tmp_path, mgr):
    nodes = Nodes(world)
    inc = add(mgr, nodes, "Creek Bend", 1, "02:00:00", S.CHAINSAW)
    mgr.update(inc.id, status="watching", next_check_at=at("02:30:00"))
    again = IncidentManager(world, mgr.files, IncidentRepo(tmp_path / "i.sqlite"))
    assert again.get(inc.id).status == "watching"
    assert again.add(nodes.record("Creek Bend", 1, at("02:00:00"), S.CHAINSAW, 0.9)[0]) is None


def test_agent_files_stay_inside_root(tmp_path):
    files = AgentFiles(tmp_path / "fs")
    with pytest.raises(ValueError):
        files.disk("/../../etc/passwd")


class Wall:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_wakes_batch_detections_for_ten_seconds():
    wall = Wall()
    q = WakeQueue(wall=wall)
    q.detection("inc", (1, 1), now=100)
    q.detection("inc", (2, 1), now=104)
    assert q.pop_due(109) == []
    (w,) = q.pop_due(110)
    assert w.kind == "new_detections" and w.new_records == [(1, 1), (2, 1)]
    assert q.pop_due(500) == []


def test_paused_sim_cannot_hold_detection_wakes_forever():
    wall = Wall()
    q = WakeQueue(wall=wall)
    q.detection("inc", (1, 1), now=100)
    wall.t = 11
    (w,) = q.pop_due(100)
    assert w.new_records == [(1, 1)]


def test_scheduled_checks_and_messages():
    q = WakeQueue(wall=Wall())
    q.schedule("inc", at=1000)
    q.schedule("inc", at=2000, note="newer")  # replaces the older check
    assert q.pop_due(1500) == []
    q.message("inc", "on site")
    (w,) = q.pop_due(1500)
    assert w.kind == "ranger_message" and w.messages == ["on site"] and not w.check_due
    (w,) = q.pop_due(2000)
    assert w.check_due and w.check_note == "newer" and w.kind == "scheduled_check"


def test_busy_incidents_keep_their_wakes():
    q = WakeQueue(wall=Wall())
    q.detection("inc", (1, 1), now=0)
    assert q.pop_due(100, skip={"inc"}) == []
    assert len(q.pop_due(100)) == 1


def test_wake_queue_has_pending_counts_detections_and_messages_but_not_scheduled_checks():
    q = WakeQueue(batch_s=10)
    assert not q.has_pending()
    q.schedule("a", at=500)
    assert not q.has_pending()
    q.detection("a", (1, 1), now=0)
    assert q.has_pending()
    assert [w.incident_id for w in q.pop_due(now=100)] == ["a"]
    assert not q.has_pending()
    q.message("a", "any news?")
    assert q.has_pending()
