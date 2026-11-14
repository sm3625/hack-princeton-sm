import math
import threading
import time
from itertools import pairwise

import pytest

from common.contracts import Route
from sim import world as worldmap
from sim.clock import SimClock
from sim.drone import (
    BATTERY_PCT_PER_KM,
    DEFAULT_OBSERVATION,
    HOVER_PCT,
    Drone,
    Observation,
    segment_hits_rect,
    shortest_path,
)
from sim.engine import SimEngine

WORLD = worldmap.load()
BASE = (WORLD.base_x_m, WORLD.base_y_m)
E4 = (2167.0, 1700.0)  # between Arroyo Recodo, Paso Musgo and Brecha Vieja
G2 = (3200.0, 900.0)  # straight line from base crosses no-fly F2


def make_drone(battery=100.0):
    drone = Drone(WORLD, SimClock(start=0, paused=True))
    drone.reset(battery)
    return drone


def test_battery_covers_out_and_back_plus_hover():
    route = make_drone().plan_route(*E4)
    one_way = math.dist(BASE, E4)
    assert route.feasible
    assert route.distance_m == pytest.approx(one_way, abs=0.5)
    expected = 2 * one_way / 1000 * BATTERY_PCT_PER_KM + HOVER_PCT
    assert route.battery_needed_pct == pytest.approx(expected, abs=0.2)


def test_far_corner_is_infeasible_because_of_reserve():
    route = make_drone().plan_route(3200, 3600)  # Cedro Viejo: needs ~93% of 100%
    assert not route.feasible
    assert 85 < route.battery_needed_pct < 100
    assert "reserve" in route.reason


def test_low_battery_makes_a_short_route_infeasible():
    assert not make_drone(battery=60).plan_route(*E4).feasible
    assert make_drone(battery=70).plan_route(*E4).feasible


def test_target_inside_no_fly_or_off_map_is_refused():
    drone = make_drone()
    inside = drone.plan_route(3400, 2600)  # Vado de Piedra sits in no-fly G6
    assert not inside.feasible and "G6" in inside.reason
    off_map = drone.plan_route(5000, 100)
    assert not off_map.feasible and "outside" in off_map.reason


def _points_along(path, step=5.0):
    for a, b in pairwise(path):
        n = max(1, int(math.dist(a, b) / step))
        for i in range(n + 1):
            f = i / n
            yield (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def test_route_avoids_no_fly_sectors():
    f2 = worldmap.sector_rect(WORLD, "F2")
    assert segment_hits_rect(BASE, G2, f2)  # the direct line would cross F2
    route = make_drone().plan_route(*G2)
    assert route.feasible
    assert len(route.waypoints) > 2
    assert route.distance_m > math.dist(BASE, G2)
    for zone in [worldmap.sector_rect(WORLD, s) for s in WORLD.no_fly]:
        for x, y in _points_along(route.waypoints):
            assert not (zone[0] < x < zone[2] and zone[1] < y < zone[3])


def test_shortest_path_returns_none_when_walled_in():
    walls = [(100, -10, 200, 1010)]
    assert shortest_path((0, 500), (300, 500), walls, (0, 0, 1000, 1000), margin=0) is None


def test_segment_hits_rect_ignores_edges_and_corners():
    r = (0, 0, 10, 10)
    assert segment_hits_rect((-5, 5), (15, 5), r)
    assert not segment_hits_rect((-5, 0), (15, 0), r)  # along an edge
    assert not segment_hits_rect((-5, 5), (0, 10), r)  # touches a corner only
    assert not segment_hits_rect((-5, 20), (15, 20), r)


class Recorder:
    def __init__(self):
        self.messages = []

    def __call__(self, msg):
        self.messages.append(msg)


def make_engine(speed=60.0):
    rec = Recorder()
    engine = SimEngine(publish=rec)
    engine.clock.set_time(0)
    engine.clock.set_speed(speed)
    engine.drone.reset(100)
    return engine, rec


def test_fly_returns_scripted_observation_and_drains_battery():
    engine, rec = make_engine()
    engine.clock.pause()
    engine.drone.set_observations([Observation("E4", "fresh clearing, one vehicle")])
    route = engine.plan_route(*E4)
    assert engine.fly(route, wait=False) == "fresh clearing, one vehicle"
    assert engine.drone_status().flying
    msg = rec.messages[-1]
    assert msg["type"] == "drone" and msg["flying"] and msg["path"][0] == list(BASE)
    assert engine.fly(route, wait=False).startswith("Not flown")  # already flying

    engine.clock.advance(route.distance_m / 10)  # 10 m/s: now over the target
    over = engine.drone_status()
    assert over.flying and math.dist((over.x_m, over.y_m), E4) < 1
    assert 100 - over.battery_pct == pytest.approx(route.distance_m / 1000 * 10, abs=0.3)

    engine.clock.advance(30 + route.distance_m / 10 + 1)  # hover, fly home, 1 s on the pad
    landed = engine.drone_status()
    assert not landed.flying and (landed.x_m, landed.y_m) == BASE
    assert 100 - landed.battery_pct == pytest.approx(route.battery_needed_pct, abs=0.3)
    assert engine.plan_route(*E4).feasible is False  # not recharged enough for a second trip


def test_observation_time_window_and_default():
    engine, _ = make_engine()
    engine.clock.pause()
    engine.drone.set_observations([Observation("E4", "vehicle", start=10_000, end=11_000)])
    assert engine.fly(engine.plan_route(*E4), wait=False) == DEFAULT_OBSERVATION


def test_infeasible_route_is_not_flown():
    engine, _ = make_engine()
    result = engine.fly(
        Route(waypoints=[(3200, 3600)], distance_m=0, battery_needed_pct=0, feasible=True)
    )
    assert result.startswith("Not flown") and "reserve" in result
    assert not engine.drone_status().flying


def test_fly_blocks_in_a_worker_thread_until_arrival():
    engine, _ = make_engine(speed=60)
    engine.drone.set_observations([Observation("B2", "campfire")])
    target = (700.0, 700.0)  # ~707 m from base: ~71 sim-s, ~1.2 s wall at 60x
    route = engine.plan_route(*target)
    out = {}

    def worker():
        t0 = time.monotonic()
        out["seen"] = engine.fly(route)
        out["wall"] = time.monotonic() - t0

    th = threading.Thread(target=worker)
    th.start()
    th.join(5)
    assert out["seen"] == "campfire"
    assert 0.8 < out["wall"] < 3.0
    x, y = engine.drone_status().x_m, engine.drone_status().y_m
    assert math.dist((x, y), target) < 30
