import asyncio
import time

import pytest

from sim.clock import SimClock


class Wall:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_runs_at_speed():
    wall = Wall()
    clock = SimClock(start=0, speed=30, wall=wall)
    wall.t += 10
    assert clock.now() == 300


def test_speed_is_clamped_to_1_to_60():
    clock = SimClock(start=0)
    assert clock.set_speed(500) == 60
    assert clock.set_speed(0) == 1


def test_pause_freezes_and_resume_does_not_jump():
    wall = Wall()
    clock = SimClock(start=0, speed=10, wall=wall)
    wall.t += 1
    clock.pause()
    wall.t += 100
    assert clock.now() == 10
    clock.resume()
    wall.t += 1
    assert clock.now() == 20


def test_speed_change_keeps_time_continuous():
    wall = Wall()
    clock = SimClock(start=0, speed=10, wall=wall)
    wall.t += 2
    clock.set_speed(60)
    assert clock.now() == 20
    wall.t += 1
    assert clock.now() == 80


def test_advance_and_set_time():
    wall = Wall()
    clock = SimClock(start=0, paused=True, wall=wall)
    clock.advance(90)
    assert clock.now() == 90
    clock.set_time(5000)
    assert clock.now() == 5000


def test_wall_seconds_until():
    wall = Wall()
    clock = SimClock(start=0, speed=30, wall=wall)
    assert clock.wall_seconds_until(300) == pytest.approx(10)
    assert clock.wall_seconds_until(-5) == 0
    clock.pause()
    assert clock.wall_seconds_until(300) is None


def test_sleep_until_follows_speed():
    clock = SimClock(start=0, speed=60)
    t0 = time.monotonic()
    asyncio.run(clock.sleep_until(6))  # 6 sim-s at 60x = 0.1 s wall
    assert 0.05 < time.monotonic() - t0 < 1.0
    assert clock.now_f() >= 6


def test_sleep_until_waits_while_paused():
    async def scenario():
        clock = SimClock(start=0, speed=60, paused=True)
        task = asyncio.create_task(clock.sleep_until(3))
        await asyncio.sleep(0.35)
        assert not task.done()
        clock.resume()
        await asyncio.wait_for(task, 1.0)

    asyncio.run(scenario())
